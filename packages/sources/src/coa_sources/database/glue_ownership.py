# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace ownership checks for the Glue Data Catalog target of a source.

A Glue source names the database it catalogs — ``catalogId`` + ``databaseName``
— and those two values are the only thing deciding which data the discovery
connector reads, samples through Athena, and (in strict Lake Formation mode)
grants itself access to. ``manageSource`` authorizes the caller against the
NAMESPACE the source is created in; it says nothing about the database. Without
a check here, a steward in any namespace can point a source at any Glue database
in the platform account — including another namespace's managed federated
catalog, whose name is visible to that namespace's viewers via
``GetSource.athenaDataCatalogName`` — and read its schema, its column comments
and a sample of its values.

Two kinds of target, two different sources of truth:

* **Platform-provisioned catalogs** (``{sanitizedPrefix}ds_*``, minted by the
  federated-JDBC path). This deployment created them, so it knows who owns them:
  source-create writes a claim keyed by catalog name, and the claim is the
  owner. No other namespace may name one.
* **Everything else** — a native Glue database that already existed. Ownership
  is unknowable from here, so the DATA OWNER declares it by tagging the database
  ``{prefix}:namespace`` with the namespace(s) allowed to catalog it. The key and the
  value format are the shared ones — ``namespace_tag_key()`` and
  ``parse_namespace_tag()`` in ``coa_common.constants``, the same pair that binds a
  JDBC source's credential secret — so a space-separated list of namespace UUIDs,
  validated identically. ``ALL`` is a Glue-only addition on top: it is not a
  namespace id, so it is matched before the shared parser runs. An untagged database is not
  catalogable, which is the fail-closed direction: the platform never decides for
  itself that it may read data nobody handed it.

  Reading that tag needs ``glue:GetTags`` **and** ``glue:GetDatabase`` on the
  catalog — Glue authorizes the former against the latter, and without both the
  check fails closed and refuses every legitimate source. See the
  ``GlueOwnershipTagRead`` statement in infra/lib/stacks/services/sources-stack.ts.

Cross-account targets are exempt. Reaching them at all requires the customer's
own ``crossAccountRoleArn``, whose trust policy is the authorization, and their
Glue tags are theirs to set — not something this deployment can require.

The check runs at four points, because each is reachable without the others:
source-create (a 403 before anything is stored), the configuration-update guard
(the stored blob is discovery's input, so a repoint would bypass create),
``_discover`` (the last gate before the connector touches Glue and Athena), and
the federation provisioner (which holds Lake Formation admin, so it verifies
rather than assumes). Create is the one caller that tolerates a database which
does not exist yet — see ``allow_missing_database``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, NamedTuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from coa_common.constants import namespace_tag_key, parse_namespace_tag

from coa_sources.client_errors import ClientSafeError, client_safe_error, redact

logger = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Tag value granting every namespace access. An explicit, deliberate act by
# whoever owns the database — the same shape as a public read on a bucket, and
# recorded in the same place — so it is honoured rather than second-guessed.
#
# `ALL`, not `*`: Glue validates tag values against `[\p{L}\p{Z}\p{N}_.:/=+\-@]*`
# and rejects `*` outright with InvalidInputException, so a `*` sentinel could
# never actually be applied to a database. Namespace ids are UUIDs, so `ALL` can
# never collide with one. Matched case-insensitively for the same reason the
# separator is permissive — an owner typing `all` means this.
SHARED_WITH_ALL = "ALL"


# Claim records live in the sources table alongside the source rows they belong
# to, keyed out of the ``NS#`` space AND written without the DAO's ``createdAt``
# stamp. Both are needed to keep a claim out of a namespace's source listing: the
# key space hides it from the base-table query, and the missing ``createdAt``
# hides it from the ``ByNamespace`` GSI, which keys on the ATTRIBUTES
# ``(namespaceId, createdAt)`` rather than on PK/SK. See
# :func:`claim_platform_catalog`.
_CLAIM_PK_PREFIX = "GLUECAT#"
_CLAIM_SK = "CLAIM"

_glue_client: Any | None = None
_account_id: str | None = None


def _get_glue() -> Any:
    global _glue_client  # noqa: PLW0603
    if _glue_client is None:
        _glue_client = boto3.client("glue", region_name=AWS_REGION)
    return _glue_client


def _deployment_account() -> str | None:
    """This deployment's account id, or ``None`` when it cannot be resolved.

    ``None`` rather than an exception because the only decision this feeds is the
    cross-account exemption: not knowing our own account means we cannot establish
    that a target is somebody else's, so the exemption does not apply and the
    caller falls through to the owner-tag check. An unresolvable identity is a
    broken environment, and the fail-closed reading of it is a refused source
    rather than a 500 — or, worse, an exemption granted by default.
    """
    global _account_id  # noqa: PLW0603
    if _account_id is None:
        try:
            _account_id = boto3.client("sts", region_name=AWS_REGION).get_caller_identity().get("Account") or ""
        except (ClientError, BotoCoreError):
            logger.warning("glue_ownership_caller_identity_failed", exc_info=True)
            return None
    return _account_id or None


def _partition(region: str) -> str:
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    if region.startswith("cn-"):
        return "aws-cn"
    return "aws"


class GlueOwnershipError(ClientSafeError):
    """The caller's namespace is not authorized for the requested Glue database.

    Carries a message written for the steward who will read it: it names the
    database, why it was refused, and the one command that fixes it. Callers turn
    it into a 403 (control plane) or a scan failure (pipeline) without rewording —
    as a :class:`ClientSafeError` it is neither truncated nor redacted on the way,
    which would break the tag command it carries. It names only the caller's own
    account, database and namespace; any underlying AWS error is reduced by
    :func:`client_safe_error` before it is embedded.
    """


# ---------------------------------------------------------------------------
# Platform-provisioned catalogs
# ---------------------------------------------------------------------------


def platform_catalog_prefix() -> str:
    """The ``{sanitizedPrefix}ds_`` shape every catalog this deployment mints carries.

    Derived exactly as ``glue_connection_provisioner.build_catalog_name`` and the
    CDK's ``fedResourcePrefix`` derive it — same env var, same default, same
    sanitisation — because a disagreement between the three would either exempt
    our own catalogs from this check or refuse databases that have nothing to do
    with us.
    """
    resource_prefix = os.environ.get("RESOURCE_PREFIX", "coa-dev-")
    return re.sub(r"[^a-z0-9]", "", resource_prefix.lower()) + "ds_"


def nested_catalog_name(catalog_id: str) -> str:
    """The nested catalog name in a ``account:catalogName`` id, else ``""``.

    A bare 12-digit ``catalogId`` addresses the account's root Data Catalog,
    which has no name and is never platform-provisioned.
    """
    catalog_id = (catalog_id or "").strip()
    return catalog_id.split(":", 1)[1] if ":" in catalog_id else ""


def is_platform_provisioned(catalog_id: str) -> bool:
    """Whether ``catalog_id`` names a catalog this deployment created."""
    return nested_catalog_name(catalog_id).startswith(platform_catalog_prefix())


def _claim_key(catalog_name: str) -> dict[str, str]:
    return {"PK": f"{_CLAIM_PK_PREFIX}{catalog_name}", "SK": _CLAIM_SK}


def claim_platform_catalog(dao: Any, *, catalog_name: str, namespace_id: str, source_id: str) -> None:
    """Record which namespace owns a catalog this deployment is about to mint.

    Written at source-create rather than at provision time, deliberately: the
    catalog name is derived from the source id, so it is known before the catalog
    exists, and claiming first means there is no window in which a real catalog has
    no owner on record.

    Best-effort at the call site — a claim that fails to write leaves the catalog
    unclaimed, which :func:`assert_namespace_may_catalog` refuses for everyone
    including its owner, rather than opening it up.

    ``auto_timestamp=False`` is load-bearing, not tidiness. The DAO otherwise
    stamps ``createdAt``, and the sources table's ``ByNamespace`` GSI is keyed
    ``(namespaceId, createdAt)`` — so the stamp, combined with the ``namespaceId``
    below, is exactly what lands a claim in a namespace's own source listing.
    Keeping the claim out of the ``NS#`` key space is NOT sufficient: a GSI keys on
    ATTRIBUTES, not on PK/SK. With no ``createdAt`` the claim is missing a GSI key
    and DynamoDB leaves it out of the index entirely (the same sparse-index
    behaviour that already keeps ``KGBUILD#`` rows out).

    Regression: with the timestamp, ``GET /sources`` 500s for any namespace holding
    a claim — ``_item_to_summary`` cannot project a claim into a ``SourceSummary``
    and pydantic rejects the four absent required fields.
    """
    dao.put(
        {**_claim_key(catalog_name), "catalogName": catalog_name, "namespaceId": namespace_id, "sourceId": source_id},
        auto_timestamp=False,
    )


def release_platform_catalog(dao: Any, *, catalog_name: str) -> None:
    """Drop a catalog claim once its source and catalog are gone.

    Not about freeing the name — it derives from the source id, so nothing else can
    ever be given it. It is about the record not outliving what it describes, which
    is the difference between an audit that reads cleanly and one full of claims on
    catalogs that no longer exist.
    """
    dao.delete(_claim_key(catalog_name))


def claim_owner(dao: Any, catalog_name: str) -> str | None:
    """Namespace that owns a platform-provisioned catalog, or ``None`` if unclaimed."""
    item = dao.get(_claim_key(catalog_name))
    return item.get("namespaceId") if item else None


# ---------------------------------------------------------------------------
# Owner-declared tags on native databases
# ---------------------------------------------------------------------------


def _database_arn(catalog_id: str, database_name: str, region: str) -> str:
    """ARN of a Glue database, nested under its catalog when it has one.

    Databases in a nested catalog are addressed ``database/{catalog}/{db}`` —
    the shape the deployment's own IAM statements use for federated catalogs.
    """
    nested = nested_catalog_name(catalog_id)
    account = (catalog_id or "").split(":", 1)[0].strip() or _deployment_account() or ""
    resource = f"database/{nested}/{database_name}" if nested else f"database/{database_name}"
    return f"arn:{_partition(region)}:glue:{region}:{account}:{resource}"


class _TagLookup(NamedTuple):
    """Outcome of reading a database's ownership tag.

    Three outcomes, not one, because they need three different messages even
    though all three deny:

    * ``namespaces`` populated — the owner has opted in, for those namespaces.
    * ``missing`` — no such database, so there is no owner to have opted in. See
      ``allow_missing_database`` in :func:`assert_namespace_may_catalog`.
    * ``unreadable`` — the tag could not be read at all. That is an operator
      problem with THIS deployment (missing ``glue:GetTags``/``glue:GetDatabase``,
      no Lake Formation ``DESCRIBE``, a throttle), not a statement about
      ownership, and telling the caller to apply a tag they may already have
      applied sends them to fix the wrong thing.
    """

    namespaces: frozenset[str]
    missing: bool = False
    unreadable: bool = False
    detail: str = ""


def _tagged_namespaces(catalog_id: str, database_name: str, region: str, glue_client: Any | None = None) -> _TagLookup:
    """Namespaces the database's owner has declared may catalog it.

    An unreadable tag set denies, like every other unresolved case here: failing
    closed on an infrastructure error costs an onboarding retry, while failing
    open would make an IAM regression silently restore the hole this check exists
    to close. What it does NOT do is claim the database is untagged — it reports
    ``unreadable`` so the caller can say what actually happened.
    """
    glue = glue_client or _get_glue()
    arn = _database_arn(catalog_id, database_name, region)
    try:
        tags = glue.get_tags(ResourceArn=arn).get("Tags", {}) or {}
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "EntityNotFoundException":
            logger.info("glue_ownership_database_absent", extra={"resource_arn": arn})
            return _TagLookup(frozenset(), missing=True)
        logger.warning("glue_get_tags_failed", extra={"resource_arn": arn}, exc_info=True)
        # The detail reaches the caller (403 / scan failure): error code only, the
        # service message (role ARN, account) stays in the log line above.
        return _TagLookup(frozenset(), unreadable=True, detail=client_safe_error(exc))
    except BotoCoreError as exc:
        logger.warning("glue_get_tags_failed", extra={"resource_arn": arn}, exc_info=True)
        # The detail reaches the caller (403 / scan failure): error code only, the
        # service message (role ARN, account) stays in the log line above.
        return _TagLookup(frozenset(), unreadable=True, detail=client_safe_error(exc))
    raw = tags.get(namespace_tag_key(), "") or ""
    # `ALL` is Glue-only and is not a namespace id, so it is recognised before the
    # shared parser sees it — only as the WHOLE value, never as a list entry. A
    # database is either shared with everyone or with a named set; "everyone plus
    # these" says nothing extra, and allowing it inside a list would mean carrying a
    # non-UUID token past a validator whose whole job is to reject those.
    if not raw.strip():
        # No tag at all — the owner has simply not opted in. Distinct from a tag that
        # exists but cannot be used: this is the case where "apply the tag" is exactly
        # the right advice, so it must not be reported as a value problem.
        return _TagLookup(frozenset())
    if raw.strip().upper() == SHARED_WITH_ALL:
        return _TagLookup(frozenset({SHARED_WITH_ALL}))
    try:
        return _TagLookup(frozenset(parse_namespace_tag(raw)))
    except ValueError as exc:
        # A tag that exists but is malformed — a non-UUID entry, a comma, padded
        # whitespace. Deny, and say the value is wrong rather than that the caller is
        # not registered: the owner needs to fix the tag, not add another one. Shares
        # `unreadable` because the operator-facing distinction is the same ("this
        # deployment could not act on the tag"), not because the cause is.
        logger.warning(
            "glue_ownership_tag_malformed",
            extra={"resource_arn": arn, "error": str(exc)},
        )
        return _TagLookup(frozenset(), unreadable=True, detail=f"tag value is not usable: {redact(str(exc))}")


def _unreadable_hint(catalog_id: str, database_name: str, region: str, detail: str) -> str:
    """Message for a tag that could not be read — an operator problem, not an owner one.

    Deliberately does NOT tell the caller to apply the tag: the tag may already be
    correct, and during this fix's own rollout exactly this failure (``GetTags``
    without ``GetDatabase``) presented as "not registered to namespace" and cost a
    deploy cycle to diagnose.
    """
    return (
        f"Could not verify which namespace owns Glue database '{database_name}', so the request was "
        f"refused. This is a problem with this deployment's access to the database, not with the "
        f"database's registration — the tag may already be correct.\n"
        f"Underlying error: {detail}\n"
        f"Check, in this order:\n"
        f"  1. The sources-api role holds BOTH glue:GetTags and glue:GetDatabase (Glue authorizes "
        f"GetTags on a database against GetDatabase on the catalog), and the discovery and "
        f"federation-provisioner roles hold glue:GetTags. Deploy the current sources stack if not.\n"
        f"  2. If the database is Lake Formation-governed, the reader also needs LF DESCRIBE:\n"
        f"     aws lakeformation grant-permissions "
        f"--principal DataLakePrincipalIdentifier=IAM_ALLOWED_PRINCIPALS "
        f'--resource \'{{"Database":{{"Name":"{database_name}"}}}}\' --permissions DESCRIBE\n'
        f"  3. That '{_database_arn(catalog_id, database_name, region)}' is the database you meant."
    )


def _tag_hint(catalog_id: str, database_name: str, namespace_id: str, region: str) -> str:
    return (
        f"Glue database '{database_name}' is not registered to namespace '{namespace_id}'. "
        f"A namespace may only catalog a database whose owner has opted in by tagging it. "
        f"As the database owner, apply the tag and retry:\n"
        f"  aws glue tag-resource --resource-arn {_database_arn(catalog_id, database_name, region)} "
        f'--tags-to-add \'{{"{namespace_tag_key()}":"{namespace_id}"}}\'\n'
        f"To share the database with several namespaces, give the tag a SPACE-separated list "
        f"(Glue rejects commas in tag values); to share it with every namespace in this "
        f"deployment, use '{SHARED_WITH_ALL}'.\n"
        f"If the database is Lake Formation-governed, the tag alone is not enough — reading it "
        f"needs LF DESCRIBE as well. Either opt the database into IAM mode:\n"
        f"  aws lakeformation grant-permissions "
        f"--principal DataLakePrincipalIdentifier=IAM_ALLOWED_PRINCIPALS "
        f'--resource \'{{"Database":{{"Name":"{database_name}"}}}}\' --permissions DESCRIBE\n'
        f"or grant DESCRIBE to this deployment's sources-api role specifically."
    )


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def assert_namespace_may_catalog(
    dao: Any,
    *,
    namespace_id: str,
    catalog_id: str,
    database_name: str,
    region: str = "",
    cross_account_role_arn: str | None = None,
    allow_missing_database: bool = False,
    glue_client: Any | None = None,
) -> None:
    """Raise :class:`GlueOwnershipError` unless ``namespace_id`` may catalog the database.

    Order matters: the cross-account exemption comes first (nothing in the
    platform account is being read, so neither rule applies), then the claim
    check for our own catalogs (authoritative, and a tag on one of those would be
    a forgeable second opinion), then the owner's tag for everything else.

    ``allow_missing_database`` lets a database that does not exist through. Set it
    at SOURCE-CREATE only, where the long-standing contract is that create persists
    the config and the async scan reports a bad target — a source may legitimately
    be registered before its database is built. Nothing is read there, so nothing
    leaks, and the same check runs again in the pipeline against a database that by
    then either exists and is owned or fails discovery on its own terms.

    It must stay ``False`` everywhere in the pipeline. There, "absent" is one
    ``CreateDatabase`` away from "present and readable", so treating it as
    permission would leave a window in which an unowned database becomes readable
    between the check and the read.
    """
    region = region or AWS_REGION
    if not database_name:
        raise GlueOwnershipError("databaseName is required")

    # The exemption turns on the target being SOMEBODY ELSE'S account, not merely
    # on a role ARN being supplied: a `crossAccountRoleArn` naming a role in this
    # account, alongside this account's `catalogId`, would otherwise exempt every
    # local database — the check, switched off by one caller-supplied field.
    account = (catalog_id or "").split(":", 1)[0].strip()
    if cross_account_role_arn and account:
        deployment_account = _deployment_account()
        if deployment_account and account != deployment_account:
            logger.info(
                "glue_ownership_cross_account_exempt",
                extra={"namespace_id": namespace_id, "catalog_account": account},
            )
            return

    if is_platform_provisioned(catalog_id):
        catalog_name = nested_catalog_name(catalog_id)
        owner = claim_owner(dao, catalog_name)
        if owner == namespace_id:
            return
        # Deliberately the same message whether the catalog belongs to another
        # namespace or to nothing at all: the caller may not learn which of its
        # guesses named a real catalog.
        raise GlueOwnershipError(
            f"Catalog '{catalog_name}' is a managed federated catalog that namespace "
            f"'{namespace_id}' does not own, so it cannot be used as a Glue source. "
            f"Catalog a federated source through its own jdbcConfiguration instead."
        )

    lookup = _tagged_namespaces(catalog_id, database_name, region, glue_client)
    shared = any(token.upper() == SHARED_WITH_ALL for token in lookup.namespaces)
    if namespace_id in lookup.namespaces or shared:
        return
    if lookup.missing and allow_missing_database:
        logger.info(
            "glue_ownership_deferred_database_absent",
            extra={"namespace_id": namespace_id, "database": database_name},
        )
        return
    # Same denial either way — only the message differs, because "we could not
    # look" and "the owner did not opt you in" send an operator to different
    # places. `allow_missing_database` deliberately does NOT extend to unreadable:
    # a tag we cannot read is not evidence the database is absent.
    if lookup.unreadable:
        raise GlueOwnershipError(_unreadable_hint(catalog_id, database_name, region, lookup.detail))
    raise GlueOwnershipError(_tag_hint(catalog_id, database_name, namespace_id, region))
