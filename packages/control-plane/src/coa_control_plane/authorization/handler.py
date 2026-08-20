# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""API Gateway custom Lambda authorizer handler.

Validates bearer tokens (JWT for humans, Agent Access Tokens for agents),
resolves roles from DynamoDB, evaluates Cedar policies, and returns an
IAM policy document with forwarded authorization context.

The API Gateway authorizer result cache is disabled (``authorizerResultTtl``
is 0), so every request reaches this handler. To keep that off the
ResourceRoleMappings GSI, resolved roles are cached in-process, guarded by
two independent bounds: the version counter in the CacheInvalidation table
(bumped by ``stream_handler`` on any grant change, so a revocation takes
effect within seconds) and a per-entry max age, so staleness stays bounded
even if the stream or the counter read is broken.
"""

from __future__ import annotations

import os
import time
from typing import Any

import structlog
from coa_authorization.policy_evaluator import evaluate
from coa_common import sanitize_principal_key
from coa_common.auth import (
    TokenAuthorizer,
    TokenValidationResult,
    build_token_authorizer,
)
from coa_common.dao import DynamoDBDAO
from coa_common.dao.base import QueryParams

from coa_control_plane.authorization.metrics import emit_authorization_deny, emit_authorizer_error

logger = structlog.get_logger(__name__)

EXECUTE_API_ACTION = "execute-api:Invoke"

# ---------------------------------------------------------------------------
# Mutating actions blocked while a namespace is ARCHIVED
# ---------------------------------------------------------------------------
# An ARCHIVED namespace is frozen (data retained for audit, no management).
# These Cedar actions are the mutating operations that must be denied while a
# namespace is ARCHIVED. The actual deny is enforced by the archived-namespace
# mutation guard (a ``forbid`` folded into ``default.cedar``, which is always
# loaded) — this set decides when the authorizer fetches namespace status and
# populates ``context.namespaceStatus`` so the guard can fire. Reads/queries
# are intentionally excluded (queries remain allowed on ARCHIVED namespaces),
# as are the lifecycle actions needed to recover an archived namespace
# (``setNamespaceStatus``, ``deleteNamespace``).
_MUTATING_ACTIONS: frozenset[str] = frozenset(
    {
        "manageSource",
        "manageMetric",
        "manageOntology",
        "manageNamespace",
        "grantPermissions",
        "manageRoles",
    }
)

_ARCHIVED_STATUS = "ARCHIVED"

# ---------------------------------------------------------------------------
# Token authorizer singleton (created on first invocation)
# ---------------------------------------------------------------------------

_token_authorizer: TokenAuthorizer | None = None


def _get_token_authorizer() -> TokenAuthorizer:
    global _token_authorizer  # noqa: PLW0603
    if _token_authorizer is not None:
        return _token_authorizer

    _token_authorizer = build_token_authorizer(
        issuer=os.environ["JWKS_ISSUER"],
        region=os.environ["AWS_REGION"],
        client_id=os.environ.get("CLIENT_ID"),
        jwks_uri=os.environ.get("JWKS_URI") or None,
        group_claim_name=os.environ.get("GROUP_CLAIM_NAME", "groups"),
    )
    return _token_authorizer


# ---------------------------------------------------------------------------
# Cache invalidation (version-based — clears stale role data)
# ---------------------------------------------------------------------------

_cache_invalidation_dao: DynamoDBDAO | None = None
_local_cache_version: int = 0
# principal cache key -> (expires_at_monotonic, global_roles, resource_roles).
# The expiry is carried in the entry itself so there is one structure to clear
# and no second dict to keep in sync.
_roles_cache: dict[str, tuple[float, list[str], list[dict[str, str]]]] = {}

# Backstop bounds on the in-process role cache. The stream-driven version
# counter is the primary invalidation path; these cap the damage when it is
# unavailable (stream throttled, DLQ'd record, CacheInvalidation read failing)
# instead of letting a warm container serve revoked roles indefinitely.
_ROLES_CACHE_TTL_DEFAULT_SECONDS = 60.0


def _parse_ttl_seconds() -> float:
    """Parse the cache TTL from the environment, failing soft to the default.

    A malformed value (e.g. ``"60s"``) must not raise at import time — that
    crashes the authorizer Lambda on cold start and denies every request. Fall
    back to the default and log instead.
    """
    raw = os.environ.get("ROLES_CACHE_TTL_SECONDS")
    if raw is None:
        return _ROLES_CACHE_TTL_DEFAULT_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Invalid ROLES_CACHE_TTL_SECONDS, using default",
            value=raw,
            default=_ROLES_CACHE_TTL_DEFAULT_SECONDS,
        )
        return _ROLES_CACHE_TTL_DEFAULT_SECONDS


_ROLES_CACHE_TTL_SECONDS = _parse_ttl_seconds()
_ROLES_CACHE_MAX_ENTRIES = 1000


def _get_cache_invalidation_dao() -> DynamoDBDAO | None:
    global _cache_invalidation_dao  # noqa: PLW0603
    if _cache_invalidation_dao is not None:
        return _cache_invalidation_dao
    table = os.environ.get("CACHE_INVALIDATION_TABLE_NAME")
    if not table:
        return None
    _cache_invalidation_dao = DynamoDBDAO(table, region=os.environ["AWS_REGION"])
    return _cache_invalidation_dao


def _check_cache_version() -> None:
    """Clear local role cache if the remote version counter has been bumped."""
    global _local_cache_version  # noqa: PLW0603
    dao = _get_cache_invalidation_dao()
    if dao is None:
        return
    try:
        item = dao.get({"PK": "CACHE_VERSION", "SK": "CURRENT"})
        remote_version = int(item.get("version", 0)) if item else 0
        if remote_version > _local_cache_version:
            _roles_cache.clear()
            _local_cache_version = remote_version
            logger.info("Role cache cleared", remote_version=remote_version)
    except Exception:
        logger.warning("Failed to check cache version, proceeding with stale cache")


# ---------------------------------------------------------------------------
# Namespace status lookup (for the ARCHIVED mutation guard)
# ---------------------------------------------------------------------------

_namespaces_dao: DynamoDBDAO | None = None

# Recognized namespace lifecycle values. Used only for defense-in-depth logging
# of unexpected/corrupt status values — the guard itself matches the exact
# string "ARCHIVED", so an unrecognized value can never relax enforcement.
# Defined locally (mirrors the Smithy ``NamespaceStatus`` enum) to keep the
# authorizer bundle lightweight — it intentionally does not depend on the
# generated server-models package. Keep in sync with namespace.smithy.
_VALID_NAMESPACE_STATUSES: frozenset[str] = frozenset({"ACTIVE", "ARCHIVED", "DELETING", "DELETED", "DELETE_FAILED"})


def _get_namespaces_dao() -> DynamoDBDAO:
    """Return the Namespaces-table DAO, raising if it cannot be configured.

    The ARCHIVED mutation guard depends on this lookup, so a missing
    ``NAMESPACES_TABLE_NAME`` / ``AWS_REGION`` must NOT silently disable the
    guard — that would fail *open* and allow mutations on archived namespaces.
    We raise instead so the caller denies the request (fail-closed).
    """
    global _namespaces_dao  # noqa: PLW0603
    if _namespaces_dao is not None:
        return _namespaces_dao
    table = os.environ.get("NAMESPACES_TABLE_NAME")
    region = os.environ.get("AWS_REGION")
    if not table or not region:
        raise RuntimeError(
            "NAMESPACES_TABLE_NAME and AWS_REGION must be configured to evaluate the ARCHIVED namespace mutation guard"
        )
    _namespaces_dao = DynamoDBDAO(table, region=region)
    return _namespaces_dao


def _get_namespace_status(namespace_id: str) -> str | None:
    """Return the lifecycle status of an existing namespace.

    Contract — the caller relies on this to act correctly:

    * Returns a **status string** when the namespace record exists.
    * Returns ``None`` *only* when the namespace record is absent — a genuine
      "not found", which the downstream handler resolves to 404. Mutating a
      namespace that does not exist has no data to protect, so the guard
      intentionally lets these through (role policies still apply).
    * **Raises** on any error condition — missing configuration (see
      :func:`_get_namespaces_dao`) or a DynamoDB failure (``dao.get`` raises) —
      so the caller fails closed rather than guessing. This makes "not found"
      (``None``) cleanly distinguishable from "error" (exception).

    Read fresh on every mutating request (never cached) so that archiving takes
    effect on the next authorizer cache miss — a stale ``ACTIVE`` would wrongly
    permit a mutation. Mutations are not the hot path (reads/queries are), so
    the extra GetItem is acceptable.
    """
    dao = _get_namespaces_dao()  # raises (fail-closed) if misconfigured
    item = dao.get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    if not item:
        return None
    status = item.get("status")
    if status is not None and status not in _VALID_NAMESPACE_STATUSES:
        # Defense-in-depth: a corrupt/unknown status cannot weaken the guard
        # (it only matches "ARCHIVED"), but we surface it so data issues are
        # visible rather than silent.
        logger.warning("Unrecognized namespace status", namespace_id=namespace_id, status=status)
    return status


# ---------------------------------------------------------------------------
# Policy builder
# ---------------------------------------------------------------------------


def _build_policy(
    principal_id: str,
    effect: str,
    method_arn: str,
    context: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": EXECUTE_API_ACTION,
                    "Effect": effect,
                    "Resource": method_arn,
                }
            ],
        },
        "context": context or {},
    }


# ---------------------------------------------------------------------------
# Role resolution from ResourceRoleMappings table
# ---------------------------------------------------------------------------


def _resolve_roles(
    principal_id: str,
    groups: list[str],
    dao: DynamoDBDAO,
) -> tuple[list[str], list[dict[str, str]]]:
    """Resolve global and resource-scoped roles for a principal.

    Queries the PrincipalIndex GSI on the ResourceRoleMappings table for:
    1. Direct user grants (principalKey = ``USER#<email>``)
    2. Group-based grants (principalKey = ``GROUP#<group>``) for each IdP group

    Returns (global_roles, resource_roles) where resource_roles is a list of
    dicts with ``role`` and ``resourceUID`` keys for Cedar evaluation.
    """
    cache_key = f"{principal_id}:{','.join(sorted(groups))}"
    cached = _roles_cache.get(cache_key)
    if cached is not None:
        # Defensive: validate the entry shape before trusting it. Anything else
        # (e.g. a 2-tuple written by a prior code version) is evicted and
        # re-queried rather than unpacked blindly.
        if isinstance(cached, tuple) and len(cached) == 3:
            expires_at, cached_global, cached_resource = cached
            if time.monotonic() < expires_at:
                return cached_global, cached_resource
            del _roles_cache[cache_key]
        else:
            del _roles_cache[cache_key]
            logger.warning("Evicted corrupt cache entry", cache_key=cache_key)

    global_roles: list[str] = []
    resource_roles: list[dict[str, str]] = []

    # Build list of principal keys to query
    principal_keys = [f"User::{sanitize_principal_key(principal_id)}"]
    principal_keys.extend(f"Group::{sanitize_principal_key(g)}" for g in groups)

    for pk in principal_keys:
        try:
            # query_all, not query: a single query returns one 1 MB page, so a
            # principal whose grants span more than that would have the roles on
            # later pages silently dropped — yielding a 403 on a namespace the
            # user holds a valid grant for. Page boundaries depend on internal
            # item ordering, so which roles vanish can change between
            # invocations, making it look intermittent and unreproducible.
            items = dao.query_all(
                QueryParams(
                    key_condition="#pk = :pk",
                    expression_values={":pk": pk},
                    expression_names={"#pk": "principalKey"},
                    index_name="PrincipalIndex",
                )
            )
        except Exception:
            logger.exception("Failed to query roles", principal_key=pk)
            raise
        for item in items:
            role = item.get("role", "")
            resource_id = item.get("resourceId", "")
            if resource_id == "GLOBAL" and role:
                global_roles.append(role)
            elif resource_id and role:
                resource_roles.append({"role": role, "resourceUID": resource_id})

    # At the cap, evict the oldest entries (insertion order — dicts preserve it)
    # rather than clearing the whole cache. A full flush would send the next
    # burst of authorization requests to the PrincipalIndex GSI all at once
    # (the API Gateway authorizer result cache is disabled), risking a DynamoDB
    # throttle spike on the auth hot path. Bounded eviction keeps the
    # runaway-memory guard without that thundering herd. Not true LRU (no
    # access-order tracking); upgrade to OrderedDict.move_to_end if hit rate
    # ever matters.
    if len(_roles_cache) >= _ROLES_CACHE_MAX_ENTRIES:
        evict_count = max(1, _ROLES_CACHE_MAX_ENTRIES // 10)
        for stale_key in list(_roles_cache)[:evict_count]:
            del _roles_cache[stale_key]
        logger.info(
            "Role cache size cap reached, evicted oldest entries",
            cap=_ROLES_CACHE_MAX_ENTRIES,
            evicted=evict_count,
        )
    _roles_cache[cache_key] = (
        time.monotonic() + _ROLES_CACHE_TTL_SECONDS,
        global_roles,
        resource_roles,
    )
    return global_roles, resource_roles


# ---------------------------------------------------------------------------
# Cedar evaluation
# ---------------------------------------------------------------------------


def _evaluate_cedar(
    principal_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    global_roles: list[str],
    resource_roles: list[dict[str, str]],
    roles_dao: DynamoDBDAO,
    context: dict[str, str] | None = None,
) -> bool:
    """Load Cedar policies for the principal's roles and evaluate."""
    policy_texts: list[str] = []

    # Collect Cedar policies from Roles table for each resolved role + default
    role_ids = {"default"}  # always include the default policy (PK=GLOBAL, SK=ROLE#default)
    role_ids.update(global_roles)
    role_ids.update(r["role"] for r in resource_roles)

    for role_id in role_ids:
        for pk in ("GLOBAL", "NAMESPACE_TEMPLATE"):
            item = roles_dao.get({"PK": pk, "SK": f"ROLE#{role_id}"})
            if item and item.get("cedarPolicy"):
                policy_texts.append(item["cedarPolicy"])
                break

    if not policy_texts:
        return False

    policies = "\n".join(policy_texts)
    # Namespace-scoped policies (data-steward, data-analyst) match on the
    # resource's ``namespace`` attribute, while owner/maintainer/view policies
    # match on the resource ``id``. For every namespace-scoped route the mapped
    # ``resource_id`` IS the namespace id, so we populate ``namespace`` with it
    # too — otherwise ``resource.namespace`` is unset and the steward/analyst
    # policies never match (they would fail closed in production even though the
    # principal holds a valid namespace role). Platform routes use ``"*"`` which
    # no namespace-scoped role can satisfy, preserving platform-admin-only access.
    result = evaluate(
        policies=policies,
        user_id=principal_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        global_roles=global_roles,
        resource_roles=resource_roles,
        resource_attrs={"namespace": resource_id},
        context=context,
    )
    return result.allowed


# ---------------------------------------------------------------------------
# Request → Cedar action mapping
# ---------------------------------------------------------------------------

# Maps (API GW resource template, HTTP method) → (Cedar action, Cedar resource type)
# See docs/authz-access-matrix.md for the full role × action permission matrix.
#
# Route conventions:
#   - Hyphenated paths (e.g., /data-sources) match the Smithy model resource names.
#   - Cedar actions follow the pattern: list, view*, manage*, delete*, grant*, query, search*.
#   - Resource type determines which Cedar entity the policy evaluates against.
_PATH_MAPPING: dict[str, dict[str, tuple[str, str]]] = {
    # ── Namespace management ───────────────────────────────────────────
    "/namespaces": {
        "GET": ("list", "Namespace"),
        "POST": ("manageNamespace", "Namespace"),
    },
    "/namespaces/{namespaceId}": {
        "GET": ("viewNamespace", "Namespace"),
        "PUT": ("manageNamespace", "Namespace"),
        "DELETE": ("deleteNamespace", "Namespace"),
    },
    "/namespaces/{namespaceId}/status": {
        "PATCH": ("setNamespaceStatus", "Namespace"),
    },
    # ── Role management (read-only) ────────────────────────────────────
    "/roles": {
        "GET": ("list", "Namespace"),
    },
    "/namespaces/{namespaceId}/roles": {
        "GET": ("viewNamespace", "Namespace"),
    },
    "/namespaces/{namespaceId}/roles/{roleId}": {
        "GET": ("viewNamespace", "Namespace"),
    },
    # ── Namespace-scoped grants ────────────────────────────────────────
    "/namespaces/{namespaceId}/grants": {
        "GET": ("viewNamespace", "Namespace"),
        "POST": ("grantPermissions", "Namespace"),
    },
    "/namespaces/{namespaceId}/grants/{grantId}": {
        "DELETE": ("grantPermissions", "Namespace"),
    },
    "/principals/{principalId}/grants": {
        "GET": ("list", "Namespace"),
    },
    # ── Platform-scoped grants ─────────────────────────────────────────
    # Granting/revoking a platform role (platform-admin, platform-viewer)
    # is a global privilege. Mapping create/delete to grantPermissions with
    # resource_id="*" means only platform-admin (global_admin.cedar permits
    # all actions) is allowed — namespace-owner requires a matching
    # resourceUID, which "*" never satisfies. Listing maps to viewNamespace
    # so platform-viewer can also read the grant list.
    "/grants": {
        "GET": ("viewNamespace", "Namespace"),
        "POST": ("grantPermissions", "Namespace"),
    },
    "/grants/{grantId}": {
        "DELETE": ("grantPermissions", "Namespace"),
    },
    # ── Unified sources (database + document) ──────────────────────────
    # Reads → viewNamespace; writes/management → manageSource. The
    # data-steward, namespace-owner, namespace-maintainer, and platform-admin
    # roles hold manageSource; data-analyst and platform-viewer do not.
    # Authorization remains namespace-scoped: the role grant's resourceUID is
    # the namespace, so the evaluated resource id/namespace are the namespaceId.
    "/namespaces/{namespaceId}/sources": {
        "GET": ("viewNamespace", "Source"),
        "POST": ("manageSource", "Source"),
    },
    "/namespaces/{namespaceId}/sources/upload-urls": {
        "POST": ("manageSource", "Source"),
    },
    "/namespaces/{namespaceId}/sources/{sourceId}": {
        "GET": ("viewNamespace", "Source"),
        "DELETE": ("manageSource", "Source"),
    },
    "/namespaces/{namespaceId}/sources/{sourceId}/rescan": {
        "POST": ("manageSource", "Source"),
    },
    "/namespaces/{namespaceId}/sources/{sourceId}/metadata": {
        "PUT": ("manageSource", "Source"),
    },
    "/namespaces/{namespaceId}/sources/{sourceId}/tables": {
        "GET": ("viewNamespace", "Source"),
    },
    "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}": {
        "GET": ("viewNamespace", "Source"),
    },
    "/namespaces/{namespaceId}/sources/{sourceId}/scan/{jobId}": {
        "GET": ("viewNamespace", "Source"),
    },
    # Resource-oriented review (replaces the legacy bulk POST /review endpoint)
    "/namespaces/{namespaceId}/sources/{sourceId}/approve": {
        "POST": ("manageSource", "Source"),
    },
    "/namespaces/{namespaceId}/sources/{sourceId}/reject": {
        "POST": ("manageSource", "Source"),
    },
    "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/review": {
        "PUT": ("manageSource", "Source"),
    },
    "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/metadata": {
        "PATCH": ("manageSource", "Source"),
    },
    "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/columns/{columnName}/review": {
        "PUT": ("manageSource", "Source"),
    },
    "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/columns/{columnName}/metadata": {
        "PATCH": ("manageSource", "Source"),
    },
    "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/keys": {
        "PATCH": ("manageSource", "Source"),
    },
    # ── Metric service ─────────────────────────────────────────────────
    # Reads → readMetric; authoring/writes → manageMetric.
    "/namespaces/{namespaceId}/metrics": {
        "GET": ("readMetric", "Metric"),
        "POST": ("manageMetric", "Metric"),
    },
    "/namespaces/{namespaceId}/metrics/{name}": {
        "GET": ("readMetric", "Metric"),
        "PUT": ("manageMetric", "Metric"),
        "DELETE": ("manageMetric", "Metric"),
    },
    "/namespaces/{namespaceId}/metrics/validate": {
        "POST": ("manageMetric", "Metric"),
    },
    "/namespaces/{namespaceId}/bulk-delete-metrics": {
        "POST": ("manageMetric", "Metric"),
    },
    "/namespaces/{namespaceId}/import-osi": {
        "POST": ("manageMetric", "Metric"),
    },
    "/namespaces/{namespaceId}/import-osi/upload-url": {
        "POST": ("manageMetric", "Metric"),
    },
    "/namespaces/{namespaceId}/import-jobs/{jobId}": {
        "GET": ("readMetric", "Metric"),
    },
    "/namespaces/{namespaceId}/export-osi": {
        "GET": ("readMetric", "Metric"),
    },
    # ── Ontology induction ─────────────────────────────────────────────
    # Induction jobs operate on the namespace's Ontology; review of generated
    # changes operates on OntologyProposal resources. Both are gated by
    # manageOntology (writes) / viewNamespace (reads), namespace-scoped.
    "/namespaces/{namespaceId}/induce": {
        "POST": ("manageOntology", "Ontology"),
    },
    "/namespaces/{namespaceId}/induce/jobs": {
        "GET": ("viewNamespace", "Ontology"),
    },
    "/namespaces/{namespaceId}/induce/jobs/{jobId}": {
        "GET": ("viewNamespace", "Ontology"),
    },
    "/namespaces/{namespaceId}/induce/datasources/induced": {
        "GET": ("viewNamespace", "Ontology"),
    },
    "/namespaces/{namespaceId}/proposals": {
        "GET": ("viewNamespace", "OntologyProposal"),
    },
    "/namespaces/{namespaceId}/proposals/{proposalId}": {
        "GET": ("viewNamespace", "OntologyProposal"),
    },
    "/namespaces/{namespaceId}/proposals/{proposalId}/accept": {
        "POST": ("manageOntology", "OntologyProposal"),
    },
    "/namespaces/{namespaceId}/proposals/{proposalId}/cancel": {
        "POST": ("manageOntology", "OntologyProposal"),
    },
    "/namespaces/{namespaceId}/proposals/{proposalId}/validate": {
        "POST": ("manageOntology", "OntologyProposal"),
    },
    "/namespaces/{namespaceId}/proposals/{proposalId}/validate/jobs/{jobId}": {
        "GET": ("viewNamespace", "OntologyProposal"),
    },
    "/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints": {
        "POST": ("manageOntology", "OntologyProposal"),
    },
    "/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints/jobs/{jobId}": {
        "GET": ("viewNamespace", "OntologyProposal"),
    },
    "/namespaces/{namespaceId}/proposals/{proposalId}/compile-constraints": {
        "POST": ("manageOntology", "OntologyProposal"),
    },
    "/namespaces/{namespaceId}/proposals/{proposalId}/upload-url": {
        "POST": ("manageOntology", "OntologyProposal"),
    },
    "/namespaces/{namespaceId}/proposals/update": {
        "POST": ("manageOntology", "OntologyProposal"),
    },
    "/namespaces/{namespaceId}/proposals/reject": {
        "POST": ("manageOntology", "OntologyProposal"),
    },
    # ── Data Layer (Serve) — runtime query/retrieval ─────────────────────
    "/namespaces/{namespaceId}/query": {
        "POST": ("query", "Namespace"),
    },
    "/namespaces/{namespaceId}/translate": {
        "POST": ("translateQuery", "Namespace"),
    },
    "/namespaces/{namespaceId}/kb/search": {
        "POST": ("searchDocuments", "Namespace"),
    },
    "/namespaces/{namespaceId}/graph/traverse": {
        "POST": ("traverseGraph", "Namespace"),
    },
    # DescribeSchema is a read of the namespace's ontology summary — gated
    # under the same ``viewNamespace`` action as the other schema/ontology
    # reads, matching MCP's ``describe_schema`` Cedar action mapping.
    "/namespaces/{namespaceId}/schema": {
        "GET": ("viewNamespace", "Namespace"),
    },
    # ── Ontology graph + catalog ────────────────────────────────────────
    # Reads are gated by viewNamespace; deleting an ontology is a write, gated
    # by manageOntology (same action as induce/accept). Without the DELETE entry
    # this route falls through to the ``denyAll`` default, which wrongly blocks
    # data-steward (who holds manageOntology, not an unconstrained-action role).
    "/namespaces/{namespaceId}/ontologies": {
        "GET": ("viewNamespace", "Ontology"),
        "DELETE": ("manageOntology", "Ontology"),
    },
    "/namespaces/{namespaceId}/ontologies/{ontologyId}": {
        "GET": ("viewNamespace", "Ontology"),
    },
    "/namespaces/{namespaceId}/ontologies/{ontologyId}/download": {
        "GET": ("viewNamespace", "Ontology"),
    },
    # Minting a presigned PUT is a write: it hands out the ability to place an
    # artifact, so it is gated like the upload itself. Matches the sibling
    # precedents at ``/proposals/{proposalId}/upload-url`` and
    # ``/sources/upload-urls``, both of which gate the URL, not just its use.
    "/namespaces/{namespaceId}/ontologies/upload-url": {
        "POST": ("manageOntology", "Ontology"),
    },
    "/namespaces/{namespaceId}/ontologies/{ontologyId}/upload": {
        "POST": ("manageOntology", "Ontology"),
    },
    # "fetch" reads from a remote source but writes the result into this
    # namespace's store, so it is a write despite the name.
    "/namespaces/{namespaceId}/ontologies/{ontologyId}/fetch": {
        "POST": ("manageOntology", "Ontology"),
    },
    "/namespaces/{namespaceId}/ontologies/{ontologyId}/ingest-from-s3": {
        "POST": ("manageOntology", "Ontology"),
    },
    # Polling a job only reveals progress, so it stays a read even though the job
    # it reports on was a write — same split as
    # ``/proposals/{proposalId}/validate/jobs/{jobId}``.
    "/namespaces/{namespaceId}/ontologies/{ontologyId}/ingest-status/{jobId}": {
        "GET": ("viewNamespace", "Ontology"),
    },
    "/namespaces/{namespaceId}/ontology/foundational": {
        "GET": ("viewNamespace", "Ontology"),
    },
    "/namespaces/{namespaceId}/ontology/foundational/{key}/load": {
        "POST": ("manageOntology", "Ontology"),
    },
    "/namespaces/{namespaceId}/proposals/{proposalId}/repair-datatypes": {
        "POST": ("manageOntology", "OntologyProposal"),
    },
    "/namespaces/{namespaceId}/graph/ontology-overview": {
        "GET": ("viewNamespace", "Ontology"),
    },
    "/namespaces/{namespaceId}/graph/search": {
        "GET": ("viewNamespace", "Ontology"),
    },
    "/namespaces/{namespaceId}/graph/class": {
        "GET": ("viewNamespace", "Ontology"),
    },
    "/namespaces/{namespaceId}/graph/object-property": {
        "GET": ("viewNamespace", "Ontology"),
    },
    "/namespaces/{namespaceId}/graph/datatype-property": {
        "GET": ("viewNamespace", "Ontology"),
    },
    # ── Platform-scoped ─────────────────────────────────────────────────
    # No {namespaceId}, so the resource id resolves to "*" (see
    # _map_request_to_cedar) and no namespace-scoped grant can ever match it.
    # ``list`` is the action that works at that scope: default.cedar permits it on
    # Namespace for every authenticated principal, the same basis
    # ``GET /namespaces`` and ``GET /roles`` already stand on. Gating this on
    # viewNamespace instead would deny every role except platform-admin, which is
    # what the unmapped default was already doing.
    "/system-health": {
        "GET": ("list", "Namespace"),
    },
}


def _map_request_to_cedar(
    http_method: str,
    resource: str,
    path_params: dict[str, str],
) -> tuple[str, str, str]:
    """Map an API Gateway request to a Cedar (action, resourceType, resourceId).

    Returns a tuple of (cedar_action, cedar_resource_type, resource_id).
    Unknown routes/methods default to ``("denyAll", "Namespace", "*")`` so a
    new endpoint that hasn't been registered here fails closed instead of
    silently inheriting read access.
    """
    route = _PATH_MAPPING.get(resource, {})
    action, resource_type = route.get(http_method, ("denyAll", "Namespace"))

    # Extract the most specific resource ID from path params
    resource_id = (
        path_params.get("namespaceId")
        or path_params.get("dataSourceId")
        or path_params.get("ontologyId")
        or path_params.get("metricId")
        or path_params.get("docSourceId")
        or "*"
    )

    return action, resource_type, resource_id


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001
    """Lambda handler for API Gateway REQUEST authorizer.

    Receives the full request context including headers, path, method,
    and path parameters — enabling Cedar action mapping from the API route.
    """
    method_arn: str = event.get("methodArn", "")
    headers: dict[str, str] = event.get("headers") or {}
    auth_token = headers.get("Authorization") or headers.get("authorization", "")
    http_method: str = (event.get("httpMethod") or "").upper()
    resource_path: str = event.get("resource", "")  # e.g. /namespaces/{id}
    path_params: dict[str, str] = event.get("pathParameters") or {}

    logger.info("Authorizer invoked", method_arn=method_arn, resource=resource_path, method=http_method)

    # 1. Check role cache version (invalidation)
    _check_cache_version()

    # 2. Validate token
    try:
        authorizer = _get_token_authorizer()
        result: TokenValidationResult = authorizer.validate(auth_token)
    except ValueError as exc:
        logger.warning("Token validation failed", error=str(exc))
        emit_authorizer_error(reason="invalid_token")
        return _build_policy("", "Deny", method_arn)
    except Exception:
        logger.exception("Unexpected error during token validation")
        emit_authorizer_error(reason="token_validation_error")
        return _build_policy("", "Deny", method_arn)

    principal_id = result.principal_id  # email
    if not principal_id:
        logger.warning("No principal in token")
        emit_authorizer_error(reason="no_principal")
        return _build_policy("", "Deny", method_arn)

    # 3. Resolve roles from ResourceRoleMappings
    region = os.environ["AWS_REGION"]
    rrm_table = os.environ.get("RESOURCE_ROLE_MAPPINGS_TABLE_NAME")
    roles_table = os.environ.get("ROLES_TABLE_NAME")

    global_roles: list[str] = []
    resource_roles: list[dict[str, str]] = []

    if rrm_table:
        rrm_dao = DynamoDBDAO(rrm_table, region=region)
        try:
            global_roles, resource_roles = _resolve_roles(principal_id, result.groups, rrm_dao)
        except Exception:
            logger.exception("Role resolution failed, denying request")
            emit_authorizer_error(reason="role_resolution_error")
            return _build_policy(principal_id, "Deny", method_arn)

    # 4. Cedar policy evaluation
    is_allowed = False
    cedar_action, _cedar_resource_type, _cedar_resource_id = _map_request_to_cedar(
        http_method, resource_path, path_params
    )

    # 4a. Non-ACTIVE-namespace mutation guard — for mutating actions on a
    # specific namespace, fetch the namespace status and surface it to Cedar
    # as request context so the guard (forbid in default.cedar) can block
    # the mutation whenever the namespace is not ACTIVE (ARCHIVED, DELETING,
    # or DELETE_FAILED). Reads and lifecycle actions
    # (setNamespaceStatus/deleteNamespace) skip this entirely, so queries
    # remain allowed regardless of namespace status.
    cedar_context: dict[str, str] | None = None
    if cedar_action in _MUTATING_ACTIONS:
        namespace_id = path_params.get("namespaceId")
        if namespace_id:
            try:
                namespace_status = _get_namespace_status(namespace_id)
            except Exception:
                logger.exception("Namespace status lookup failed, denying request")
                emit_authorizer_error(reason="namespace_lookup_error", action=cedar_action)
                return _build_policy(principal_id, "Deny", method_arn)
            if namespace_status:
                cedar_context = {"namespaceStatus": namespace_status}
            # namespace_status is None only when the namespace record does not
            # exist (see _get_namespace_status). We intentionally leave the
            # context unset and let Cedar's role policies evaluate normally —
            # the downstream handler resolves a missing namespace to 404, and
            # there is no archived data to protect on a namespace that does not
            # exist. (Configuration/DynamoDB errors raise above and fail closed.)

    if roles_table:
        roles_dao = DynamoDBDAO(roles_table, region=region)

        try:
            is_allowed = _evaluate_cedar(
                principal_id=principal_id,
                action=cedar_action,
                resource_type=_cedar_resource_type,
                resource_id=_cedar_resource_id,
                global_roles=global_roles,
                resource_roles=resource_roles,
                roles_dao=roles_dao,
                context=cedar_context,
            )
        except Exception:
            logger.exception("Cedar policy evaluation failed, denying request")
            emit_authorizer_error(reason="cedar_evaluation_error", action=cedar_action)
        else:
            if not is_allowed:
                deny_reason = "deny_all_unmapped_route" if cedar_action == "denyAll" else "cedar_deny"
                emit_authorization_deny(action=cedar_action, reason=deny_reason)
    else:
        # No roles table configured — deny unless explicitly opted in to dev mode
        if os.environ.get("ALLOW_WITHOUT_ROLES", "").lower() == "true":
            logger.warning("Dev mode: allowing authenticated request without role evaluation")
            is_allowed = True
        else:
            emit_authorizer_error(reason="roles_table_not_configured", action=cedar_action)

    effect = "Allow" if is_allowed else "Deny"

    # API GW caches the authorizer response by JWT for the configured TTL.
    # The cached IAM policy must cover every method on the stage, otherwise
    # the first request locks the cache to that specific method ARN and
    # subsequent methods on the same JWT 403 until TTL expires. Cedar
    # already enforced the (action, resource) for this request.
    resource_arn = method_arn
    if effect == "Allow":
        arn_prefix = method_arn.split("/", 1)[0]
        resource_arn = f"{arn_prefix}/*"

    auth_context = {
        "userId": principal_id,
        "sub": result.sub,
        "email": result.email,
        "groups": ",".join(result.groups),
        "globalRoles": ",".join(global_roles),
        "tokenType": result.token_type.value,
    }
    if result.agent_identity:
        auth_context["agentIdentity"] = result.agent_identity

    logger.info(
        "Authorization decision",
        # Use the opaque OIDC sub (stable, non-PII) instead of principal_id
        # (email). Log group count, not the full membership list.
        principal_sub=result.sub,
        effect=effect,
        group_count=len(result.groups),
        global_roles=global_roles,
    )

    return _build_policy(principal_id, effect, resource_arn, auth_context)
