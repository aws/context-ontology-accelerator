# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authorization unit tests verifying Cedar policy security invariants."""

import pytest
from coa_authorization.policy_evaluator import evaluate, load_seed_policies
from coa_control_plane.authorization.handler import _map_request_to_cedar

pytestmark = pytest.mark.unit

POLICIES = load_seed_policies()
NS_ID = "11111111-1111-1111-1111-111111111111"


def _eval(
    action: str,
    resource_type: str = "Namespace",
    resource_id: str = NS_ID,
    global_roles: list[str] | None = None,
    resource_roles: list[dict[str, str]] | None = None,
    resource_attrs: dict | None = None,
    context: dict | None = None,
) -> bool:
    result = evaluate(
        policies=POLICIES,
        user_id="user-1",
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        global_roles=global_roles,
        resource_roles=resource_roles,
        resource_attrs=resource_attrs,
        context=context,
    )
    return result.allowed


# ─── Global Admin ────────────────────────────────────────────────────────────


class TestGlobalAdmin:
    """Global Admin has unrestricted access."""

    @pytest.mark.parametrize(
        "action",
        [
            "viewNamespace",
            "list",
            "query",
            "readMetric",
            "searchDocuments",
            "manageSource",
            "manageOntology",
            "manageMetric",
            "manageNamespace",
            "deleteNamespace",
            "grantPermissions",
            "manageRoles",
        ],
    )
    def test_admin_can_do_everything(self, action: str):
        assert _eval(action, global_roles=["platform-admin"]) is True

    def test_admin_can_manage_any_namespace(self):
        assert (
            _eval(
                "deleteNamespace", resource_id="99999999-9999-9999-9999-999999999999", global_roles=["platform-admin"]
            )
            is True
        )


# ─── Global Viewer ───────────────────────────────────────────────────────────


class TestGlobalViewer:
    """Global Viewer has read-only access."""

    @pytest.mark.parametrize(
        "action",
        ["viewNamespace", "list", "query", "readMetric", "searchDocuments"],
    )
    def test_viewer_can_read(self, action: str):
        assert _eval(action, global_roles=["platform-viewer"]) is True

    @pytest.mark.parametrize(
        "action",
        [
            "manageSource",
            "manageOntology",
            "manageMetric",
            "manageNamespace",
            "deleteNamespace",
            "grantPermissions",
            "manageRoles",
        ],
    )
    def test_viewer_cannot_write(self, action: str):
        assert _eval(action, global_roles=["platform-viewer"]) is False


# ─── Namespace Owner ─────────────────────────────────────────────────────────


class TestNamespaceOwner:
    """Namespace Owner has full access within their namespace."""

    ROLES = [{"role": "namespace-owner", "resourceUID": NS_ID}]

    @pytest.mark.parametrize(
        "action",
        [
            "viewNamespace",
            "list",
            "manageNamespace",
            "deleteNamespace",
            "grantPermissions",
            "manageRoles",
        ],
    )
    def test_owner_can_manage_namespace(self, action: str):
        assert _eval(action, resource_roles=self.ROLES) is True

    def test_owner_cannot_access_other_namespace(self):
        assert (
            _eval("manageNamespace", resource_id="99999999-9999-9999-9999-999999999999", resource_roles=self.ROLES)
            is False
        )


# ─── Namespace DataSteward ───────────────────────────────────────────────────


class TestNamespaceDataSteward:
    """DataSteward can manage data assets and read within their namespace."""

    ROLES = [{"role": "data-steward", "resourceUID": NS_ID}]

    @pytest.mark.parametrize(
        "action",
        ["manageSource", "manageOntology", "manageMetric"],
    )
    def test_steward_can_manage_data_assets(self, action: str):
        assert (
            _eval(
                action,
                resource_type="Source",
                resource_id="ds-1",
                resource_roles=self.ROLES,
                resource_attrs={"namespace": NS_ID},
            )
            is True
        )

    @pytest.mark.parametrize("action", ["query", "readMetric", "searchDocuments"])
    def test_steward_can_read(self, action: str):
        assert (
            _eval(
                action,
                resource_type="Source",
                resource_id="tbl-1",
                resource_roles=self.ROLES,
                resource_attrs={"namespace": NS_ID},
            )
            is True
        )

    def test_steward_can_view_namespace(self):
        assert _eval("viewNamespace", resource_roles=self.ROLES) is True

    def test_steward_can_list(self):
        assert _eval("list", resource_roles=self.ROLES) is True

    @pytest.mark.parametrize(
        "action",
        ["manageNamespace", "deleteNamespace", "grantPermissions", "manageRoles"],
    )
    def test_steward_cannot_manage_namespace(self, action: str):
        assert _eval(action, resource_roles=self.ROLES) is False

    def test_steward_cannot_access_other_namespace(self):
        assert (
            _eval(
                "manageSource",
                resource_type="Source",
                resource_id="ds-1",
                resource_roles=self.ROLES,
                resource_attrs={"namespace": "99999999-9999-9999-9999-999999999999"},
            )
            is False
        )


# ─── Namespace DataAnalyst ───────────────────────────────────────────────────


class TestNamespaceDataAnalyst:
    """DataAnalyst can only read/query within their namespace."""

    ROLES = [{"role": "data-analyst", "resourceUID": NS_ID}]

    @pytest.mark.parametrize("action", ["query", "readMetric", "searchDocuments"])
    def test_analyst_can_read(self, action: str):
        assert (
            _eval(
                action,
                resource_type="Source",
                resource_id="tbl-1",
                resource_roles=self.ROLES,
                resource_attrs={"namespace": NS_ID},
            )
            is True
        )

    def test_analyst_can_view_namespace(self):
        assert _eval("viewNamespace", resource_roles=self.ROLES) is True

    def test_analyst_can_list(self):
        assert _eval("list", resource_roles=self.ROLES) is True

    @pytest.mark.parametrize(
        "action",
        [
            "manageSource",
            "manageOntology",
            "manageMetric",
            "manageNamespace",
            "deleteNamespace",
            "grantPermissions",
            "manageRoles",
        ],
    )
    def test_analyst_cannot_write(self, action: str):
        assert _eval(action, resource_roles=self.ROLES) is False

    def test_analyst_cannot_access_other_namespace(self):
        assert (
            _eval(
                "query",
                resource_type="Source",
                resource_id="tbl-1",
                resource_roles=self.ROLES,
                resource_attrs={"namespace": "99999999-9999-9999-9999-999999999999"},
            )
            is False
        )


# ─── No Role (unauthenticated/no permissions) ───────────────────────────────


class TestNoRole:
    """User with no roles should be denied everything except list on Namespace."""

    @pytest.mark.parametrize(
        "action",
        [
            "viewNamespace",
            "query",
            "readMetric",
            "searchDocuments",
            "manageSource",
            "manageOntology",
            "manageMetric",
            "manageNamespace",
            "deleteNamespace",
            "grantPermissions",
            "manageRoles",
        ],
    )
    def test_no_role_denied(self, action: str):
        assert _eval(action) is False

    def test_any_user_can_list_namespaces(self):
        assert _eval("list") is True


# ─── Doc Source Namespace Isolation ─────────────────────────────────────────


NS_B = "22222222-2222-2222-2222-222222222222"


class TestSourceNamespaceIsolation:
    """Cedar policies must enforce namespace-scoped isolation for sources.

    A data-steward granted access to NS_A must not be able to manage sources
    in NS_B, and a principal with no roles must be denied entirely.
    """

    ROLES_NS_A = [{"role": "data-steward", "resourceUID": NS_ID}]

    # ── Positive: steward can manage doc sources in their own namespace ──

    def test_steward_can_create_doc_source_in_own_namespace(self):
        assert (
            _eval(
                "manageSource",
                resource_type="Source",
                resource_id="ds-1",
                resource_roles=self.ROLES_NS_A,
                resource_attrs={"namespace": NS_ID},
            )
            is True
        )

    def test_steward_can_delete_doc_source_in_own_namespace(self):
        assert (
            _eval(
                "manageSource",
                resource_type="Source",
                resource_id="ds-2",
                resource_roles=self.ROLES_NS_A,
                resource_attrs={"namespace": NS_ID},
            )
            is True
        )

    def test_steward_can_rescan_doc_source_in_own_namespace(self):
        assert (
            _eval(
                "manageSource",
                resource_type="Source",
                resource_id="ds-3",
                resource_roles=self.ROLES_NS_A,
                resource_attrs={"namespace": NS_ID},
            )
            is True
        )

    # ── Negative: steward cannot manage doc sources in a different namespace ──

    def test_steward_cannot_manage_doc_source_in_other_namespace(self):
        assert (
            _eval(
                "manageSource",
                resource_type="Source",
                resource_id="ds-other",
                resource_roles=self.ROLES_NS_A,
                resource_attrs={"namespace": NS_B},
            )
            is False
        )

    def test_steward_ns_b_cannot_access_doc_source_in_ns_a(self):
        roles_ns_b = [{"role": "data-steward", "resourceUID": NS_B}]
        assert (
            _eval(
                "manageSource",
                resource_type="Source",
                resource_id="ds-in-a",
                resource_roles=roles_ns_b,
                resource_attrs={"namespace": NS_ID},
            )
            is False
        )

    def test_no_role_cannot_manage_doc_source(self):
        assert (
            _eval(
                "manageSource",
                resource_type="Source",
                resource_id="ds-1",
                resource_attrs={"namespace": NS_ID},
            )
            is False
        )

    def test_analyst_cannot_manage_doc_source(self):
        """data-analyst is read-only; must not be able to create/delete doc sources."""
        analyst_roles = [{"role": "data-analyst", "resourceUID": NS_ID}]
        assert (
            _eval(
                "manageSource",
                resource_type="Source",
                resource_id="ds-1",
                resource_roles=analyst_roles,
                resource_attrs={"namespace": NS_ID},
            )
            is False
        )

    def test_cross_namespace_grant_provides_no_access(self):
        """Having a role in NS_B should not grant any doc-source access in NS_A."""
        roles_ns_b = [{"role": "data-steward", "resourceUID": NS_B}]
        assert (
            _eval(
                "manageSource",
                resource_type="Source",
                resource_id="ds-1",
                resource_roles=roles_ns_b,
                resource_attrs={"namespace": NS_ID},
            )
            is False
        )


# ─── Platform grant authorization ───────────────────────────────────────────


class TestPlatformGrantAuthorization:
    """Granting/revoking PLATFORM-scoped roles uses resource_id="*" so that
    only platform-admin (global_admin permits all) may write. Namespace owners
    (matched by resourceUID) and platform-viewers must be denied the write."""

    def test_admin_can_grant_platform_role(self):
        assert _eval("grantPermissions", resource_id="*", global_roles=["platform-admin"]) is True

    def test_viewer_cannot_grant_platform_role(self):
        assert _eval("grantPermissions", resource_id="*", global_roles=["platform-viewer"]) is False

    def test_namespace_owner_cannot_grant_platform_role(self):
        # A namespace owner's resourceUID is a specific namespace, never "*".
        owner_roles = [{"role": "namespace-owner", "resourceUID": NS_ID}]
        assert _eval("grantPermissions", resource_id="*", resource_roles=owner_roles) is False

    def test_no_role_cannot_grant_platform_role(self):
        assert _eval("grantPermissions", resource_id="*") is False

    def test_admin_can_view_platform_grants(self):
        assert _eval("viewNamespace", resource_id="*", global_roles=["platform-admin"]) is True

    def test_viewer_can_view_platform_grants(self):
        assert _eval("viewNamespace", resource_id="*", global_roles=["platform-viewer"]) is True

    def test_no_role_cannot_view_platform_grants(self):
        assert _eval("viewNamespace", resource_id="*") is False


class TestPlatformGrantRouteMapping:
    """The authorizer must map the /grants routes to the intended Cedar actions
    so they do not fail closed as denyAll and enforce the right privilege."""

    def test_post_grants_maps_to_grant_permissions(self):
        action, _rtype, resource_id = _map_request_to_cedar("POST", "/grants", {})
        assert action == "grantPermissions"
        assert resource_id == "*"

    def test_get_grants_maps_to_view(self):
        action, _rtype, resource_id = _map_request_to_cedar("GET", "/grants", {})
        assert action == "viewNamespace"
        assert resource_id == "*"

    def test_delete_grant_maps_to_grant_permissions(self):
        action, _rtype, resource_id = _map_request_to_cedar("DELETE", "/grants/{grantId}", {"grantId": "abc"})
        assert action == "grantPermissions"
        assert resource_id == "*"


# ─── Non-ACTIVE namespace mutation guard ─────────────────────────────────────

_MUTATING = [
    "manageSource",
    "manageMetric",
    "manageOntology",
    "manageNamespace",
    "grantPermissions",
    "manageRoles",
]
_READS = ["viewNamespace", "list", "query", "readMetric", "searchDocuments"]
_NON_ACTIVE_STATUSES = ["ARCHIVED", "DELETING", "DELETE_FAILED"]
_ARCHIVED = {"namespaceStatus": "ARCHIVED"}
_DELETING = {"namespaceStatus": "DELETING"}
_DELETE_FAILED = {"namespaceStatus": "DELETE_FAILED"}
_ACTIVE = {"namespaceStatus": "ACTIVE"}


class TestNonActiveNamespaceGuard:
    """A namespace that is not ACTIVE (ARCHIVED, DELETING, or DELETE_FAILED)
    forbids all mutating actions — overriding every permit, including
    namespace-owner and platform-admin. Reads and the recovery lifecycle
    actions (setNamespaceStatus, deleteNamespace) remain allowed. The forbid
    fires whenever context.namespaceStatus is present and != "ACTIVE".

    DELETING/DELETE_FAILED are blocked (not just ARCHIVED) so a create/update
    can never race against the async deletion pipeline's cleanup steps or
    against a retry of a failed deletion.
    """

    OWNER = [{"role": "namespace-owner", "resourceUID": NS_ID}]

    @pytest.mark.parametrize("action", _MUTATING)
    @pytest.mark.parametrize("status", _NON_ACTIVE_STATUSES)
    def test_owner_blocked_from_mutations_when_not_active(self, action: str, status: str):
        assert _eval(action, resource_roles=self.OWNER, context={"namespaceStatus": status}) is False

    @pytest.mark.parametrize("action", _MUTATING)
    @pytest.mark.parametrize("status", _NON_ACTIVE_STATUSES)
    def test_admin_blocked_from_mutations_when_not_active(self, action: str, status: str):
        # forbid overrides even the global_admin permit-all policy.
        assert _eval(action, global_roles=["platform-admin"], context={"namespaceStatus": status}) is False

    @pytest.mark.parametrize("action", _MUTATING)
    def test_mutations_allowed_when_active(self, action: str):
        assert _eval(action, resource_roles=self.OWNER, context=_ACTIVE) is True

    @pytest.mark.parametrize("action", _MUTATING)
    def test_mutations_allowed_when_no_status_context(self, action: str):
        # Without a namespaceStatus fact the guard must not fire (fail-open is
        # acceptable here because the authorizer always supplies status for
        # mutating routes that carry a namespaceId).
        assert _eval(action, resource_roles=self.OWNER, context=None) is True

    @pytest.mark.parametrize("action", _READS)
    @pytest.mark.parametrize("status", _NON_ACTIVE_STATUSES)
    def test_reads_allowed_when_not_active(self, action: str, status: str):
        assert _eval(action, resource_roles=self.OWNER, context={"namespaceStatus": status}) is True

    def test_set_namespace_status_allowed_when_archived(self):
        # Reactivation (ARCHIVED -> ACTIVE) must remain possible.
        assert _eval("setNamespaceStatus", resource_roles=self.OWNER, context=_ARCHIVED) is True

    def test_delete_namespace_allowed_when_archived(self):
        # Deletion is only legal from ARCHIVED — must not be blocked.
        assert _eval("deleteNamespace", resource_roles=self.OWNER, context=_ARCHIVED) is True

    def test_delete_namespace_allowed_when_delete_failed(self):
        # Retry (DELETE_FAILED -> DELETING) must not be blocked.
        assert _eval("deleteNamespace", resource_roles=self.OWNER, context=_DELETE_FAILED) is True


class TestArchivedGuardRouteMapping:
    """The status endpoint must map to its own action so the mutation guard
    does not block reactivation, and so it is distinct from manageNamespace."""

    def test_patch_status_maps_to_set_namespace_status(self):
        action, rtype, resource_id = _map_request_to_cedar(
            "PATCH", "/namespaces/{namespaceId}/status", {"namespaceId": NS_ID}
        )
        assert action == "setNamespaceStatus"
        assert rtype == "Namespace"
        assert resource_id == NS_ID


# ─── Schema-visibility actions on the resource types the authorizer uses ─────
# ADR: decompose-viewnamespace-schema-visibility. The authorizer evaluates
# viewSource on Source and Metric (source reads, OSI export, import jobs, the
# canViewSource flag) and viewOntology on Ontology, OntologyProposal and
# Namespace (/schema). Evaluated against the REAL seed .cedar files.

_SCHEMA_VIEW_CHECKS = [
    ("viewSource", "Source"),
    ("viewSource", "Metric"),
    ("viewOntology", "Ontology"),
    ("viewOntology", "OntologyProposal"),
    ("viewOntology", "Namespace"),
]
OTHER_NS_ID = "22222222-2222-2222-2222-222222222222"

# (label, global_roles, resource_roles, expected)
_SCHEMA_VIEW_CALLERS = [
    ("data-analyst", [], [{"role": "data-analyst", "resourceUID": NS_ID}], False),
    ("data-steward", [], [{"role": "data-steward", "resourceUID": NS_ID}], True),
    ("namespace-owner", [], [{"role": "namespace-owner", "resourceUID": NS_ID}], True),
    ("namespace-maintainer", [], [{"role": "namespace-maintainer", "resourceUID": NS_ID}], True),
    ("platform-viewer", ["platform-viewer"], [], True),
    ("platform-admin", ["platform-admin"], [], True),
    ("steward-other-namespace", [], [{"role": "data-steward", "resourceUID": OTHER_NS_ID}], False),
    ("no-role", [], [], False),
]


class TestSchemaViewSeedMatrix:
    @pytest.mark.parametrize(("action", "resource_type"), _SCHEMA_VIEW_CHECKS)
    @pytest.mark.parametrize(
        ("label", "global_roles", "resource_roles", "expected"),
        _SCHEMA_VIEW_CALLERS,
        ids=[c[0] for c in _SCHEMA_VIEW_CALLERS],
    )
    def test_schema_view(self, action, resource_type, label, global_roles, resource_roles, expected):
        allowed = _eval(
            action,
            resource_type=resource_type,
            resource_id=NS_ID,
            global_roles=global_roles,
            resource_roles=resource_roles,
            resource_attrs={"namespace": NS_ID},
        )
        assert allowed is expected, f"{label}: {action} on {resource_type}"

    @pytest.mark.parametrize(("action", "resource_type"), _SCHEMA_VIEW_CHECKS)
    def test_analyst_still_reads_metrics_where_schema_is_denied(self, action, resource_type):
        # Positive control: the analyst denial above is specific to the schema
        # actions, not a broken grant -- the same caller may read metrics.
        roles = [{"role": "data-analyst", "resourceUID": NS_ID}]
        assert _eval("readMetric", "Metric", NS_ID, resource_roles=roles, resource_attrs={"namespace": NS_ID})
        assert not _eval(action, resource_type, NS_ID, resource_roles=roles, resource_attrs={"namespace": NS_ID})
