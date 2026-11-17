# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""End-to-end security invariants: grant rows → resolved profile → firewall decision.

The per-unit suites (``test_role_resolver.py``, ``test_sql_firewall.py``) each cover
their own half, but the halves can agree individually and still compose into a
bypass: the resolver can emit a restriction the firewall then declines to enforce.
Each test therefore asserts on the decision (denied / allowed), never on an
intermediate field.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_serve import role_resolver
from coa_serve.tier2.cedar_authorizer import CedarDecision, RealCedarAuthorizer
from coa_serve.tier2.sql_firewall import SQLFirewall

_NS = "ns-Z"
_USER = "bob@x.com"

# A role Cedar permits for `query` on the namespace, so these tests exercise the
# DATA layer rather than tripping the action layer first.
_CEDAR_OK_ROLE = "namespace-owner"


def _decide(grants: list[dict], sql: str, *, namespace: str = _NS) -> tuple[bool, str | None]:
    """Resolve a profile from ``grants``, then run ``sql`` through the firewall.

    Returns ``(denied, reason)`` — the observable outcome a caller would see.
    """
    dao = MagicMock()
    dao.query_all.side_effect = lambda params: grants
    with (
        patch.object(role_resolver, "_RRM_TABLE", "rrm"),
        patch.object(role_resolver, "DynamoDBDAO", return_value=dao),
    ):
        resolved = role_resolver.resolve_profile(_USER, [], namespace=namespace)

    profile: dict = {}
    resolved.inject_into(profile)
    result = SQLFirewall().evaluate(sql, profile=profile, namespace=namespace)
    return result.denied, result.reason


def _grant(role: str = _CEDAR_OK_ROLE, **fields) -> dict:
    return {"role": role, "resourceId": _NS, **fields}


_PLATFORM_ADMIN = {"role": "platform-admin", "resourceId": "GLOBAL"}
_PLATFORM_VIEWER = {"role": "platform-viewer", "resourceId": "GLOBAL"}


@pytest.mark.unit
class TestColumnDenylistIsEnforced:
    """A denied column must be unreadable, whatever else the principal holds."""

    DENY_SSN = {"columnDenylist": {"customers": ["ssn"]}}

    def test_denied_column_is_blocked(self):
        denied, reason = _decide([_grant(**self.DENY_SSN)], "SELECT ssn FROM customers")

        assert denied, "a denylisted column must not be readable"
        assert "ssn" in (reason or "")

    def test_permitted_column_still_readable(self):
        """The denylist must not become a blanket deny."""
        denied, _ = _decide([_grant(**self.DENY_SSN)], "SELECT name FROM customers")

        assert not denied

    def test_platform_admin_cannot_read_a_denied_column(self):
        denied, _ = _decide([_grant(**self.DENY_SSN), _PLATFORM_ADMIN], "SELECT ssn FROM customers")

        assert denied, "platform-admin must not bypass the data layer"

    def test_platform_viewer_cannot_read_a_denied_column(self):
        """platform-viewer is the role the docs recommend for auditors.

        It is read-only and cannot grant itself anything, so its restrictions are a
        real boundary — an auditor should see structure without raw PII.
        """
        denied, _ = _decide([_grant(**self.DENY_SSN), _PLATFORM_VIEWER], "SELECT ssn FROM customers")

        assert denied, "platform-viewer must not bypass the data layer"

    def test_an_unrestricted_grant_on_the_same_namespace_cannot_lift_it(self):
        """A second unrestricted grant must not lift a denylist."""
        denied, _ = _decide(
            [_grant(role="data-analyst", **self.DENY_SSN), _grant(role="namespace-owner")],
            "SELECT ssn FROM customers",
        )

        assert denied, "a second unrestricted grant must not erase the denylist"

    def test_denylists_from_separate_grants_both_apply(self):
        grants = [
            _grant(role="data-analyst", columnDenylist={"customers": ["ssn"]}),
            _grant(role="reviewer", columnDenylist={"customers": ["dob"]}),
        ]

        assert _decide(grants, "SELECT ssn FROM customers")[0]
        assert _decide(grants, "SELECT dob FROM customers")[0]
        assert not _decide(grants, "SELECT name FROM customers")[0]

    def test_case_variation_does_not_bypass(self):
        denied, _ = _decide([_grant(**self.DENY_SSN)], "SELECT SSN FROM CUSTOMERS")

        assert denied, "SQL identifiers are case-insensitive; the denylist must be too"

    def test_restrictions_do_not_leak_into_another_namespace(self):
        """Restrictions are namespace-scoped — they must not over-apply either.

        Needs a grant on the other namespace too: without one, Cedar denies at the
        ACTION layer first and the data layer is never reached, so the test would
        pass for the wrong reason.
        """
        grants = [
            _grant(role="data-analyst", **self.DENY_SSN),  # ns-Z: ssn denied
            {"role": _CEDAR_OK_ROLE, "resourceId": "ns-OTHER"},  # ns-OTHER: unrestricted
        ]

        denied, _ = _decide(grants, "SELECT ssn FROM customers", namespace="ns-OTHER")

        assert not denied, "a denylist on ns-Z must not restrict ns-OTHER"


@pytest.mark.unit
class TestTableAllowlistIsEnforced:
    def test_unlisted_table_is_blocked(self):
        denied, reason = _decide([_grant(tableAllowlist=["customers"])], "SELECT * FROM orders")

        assert denied
        assert "orders" in (reason or "")

    def test_listed_table_is_allowed(self):
        denied, _ = _decide([_grant(tableAllowlist=["customers"])], "SELECT * FROM customers")

        assert not denied

    def test_platform_admin_cannot_reach_an_unlisted_table(self):
        denied, _ = _decide([_grant(tableAllowlist=["customers"]), _PLATFORM_ADMIN], "SELECT * FROM orders")

        assert denied

    def test_an_unrestricted_grant_cannot_widen_the_allowlist(self):
        denied, _ = _decide(
            [_grant(role="data-analyst", tableAllowlist=["customers"]), _grant(role="namespace-owner")],
            "SELECT * FROM orders",
        )

        assert denied, "absence of an allowlist must add no constraint, not remove one"

    def test_allowlists_from_separate_grants_both_apply(self):
        grants = [
            _grant(role="data-analyst", tableAllowlist=["customers"]),
            _grant(role="reviewer", tableAllowlist=["orders"]),
        ]

        assert not _decide(grants, "SELECT * FROM customers")[0]
        assert not _decide(grants, "SELECT * FROM orders")[0]
        assert _decide(grants, "SELECT * FROM payments")[0]

    def test_explicitly_empty_allowlist_denies_every_table(self):
        """Regression: an empty list is a restriction, not the absence of one.

        The resolver emits ``[]`` when a grant explicitly declares an empty
        allowlist. The firewall previously treated it as falsy and allowed
        everything, so "no tables permitted" silently became "all tables
        permitted". Asserted on the decision because the profile-level assertion
        passed throughout.
        """
        denied, _ = _decide([_grant(tableAllowlist=[])], "SELECT * FROM customers")

        assert denied, "an explicitly empty allowlist must deny every table"

    def test_no_allowlist_anywhere_leaves_tables_unrestricted(self):
        """The counterpart: absence really does mean unrestricted."""
        denied, _ = _decide([_grant()], "SELECT * FROM anything")

        assert not denied


@pytest.mark.unit
class TestFailClosed:
    """A failed grant read must never widen access."""

    def test_grant_lookup_failure_propagates(self):
        """Restrictions computed from a partial read are more permissive than truth."""
        dao = MagicMock()
        dao.query_all.side_effect = RuntimeError("dynamodb throttled")
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=dao),
            pytest.raises(RuntimeError),
        ):
            role_resolver.resolve_profile(_USER, [], namespace=_NS)

    def test_every_grant_page_is_read(self):
        """Grants past the first 1 MB page must not be dropped.

        A restricted grant on a later page while an unrestricted one survives would
        otherwise make the merge see no restrictions at all.
        """
        first_page = {"Items": [_grant(role="namespace-owner")], "LastEvaluatedKey": {"principalKey": "p"}}
        second_page = {"Items": [_grant(role="data-analyst", columnDenylist={"customers": ["ssn"]})]}
        table = MagicMock()
        table.query.side_effect = lambda **kwargs: second_page if "ExclusiveStartKey" in kwargs else first_page
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch("coa_common.dao.dynamodb.boto3.resource") as resource,
        ):
            resource.return_value.Table.return_value = table
            resolved = role_resolver.resolve_profile(_USER, [], namespace=_NS)

        profile: dict = {}
        resolved.inject_into(profile)
        result = SQLFirewall().evaluate("SELECT ssn FROM customers", profile=profile, namespace=_NS)
        assert result.denied, "a restricted grant on a later page must still be enforced"


def _admit(grants: list[dict], *, namespace: str = _NS) -> tuple[bool, str | None]:
    """Resolve a profile from ``grants``, then run the SQL-free namespace gate.

    Returns ``(denied, reason)`` — the observable admission outcome. Uses a
    fail-closed RealCedarAuthorizer so the no-roles case is deterministic
    regardless of ambient ``SCL_CEDAR_FAIL_OPEN_NO_ROLES``.
    """
    dao = MagicMock()
    dao.query_all.side_effect = lambda params: grants
    with (
        patch.object(role_resolver, "_RRM_TABLE", "rrm"),
        patch.object(role_resolver, "DynamoDBDAO", return_value=dao),
    ):
        resolved = role_resolver.resolve_profile(_USER, [], namespace=namespace)

    profile: dict = {}
    resolved.inject_into(profile)
    firewall = SQLFirewall(cedar_authorizer=RealCedarAuthorizer(fail_open_no_roles=False))
    result = firewall.authorize_namespace(namespace, profile)
    return result.denied, result.reason


@pytest.mark.unit
class TestNamespaceAdmissionGate:
    """CWE-862: the SQL-free retrieval surfaces must be namespace-gated too.

    ``authorize_namespace`` is the single admission point for the isolated
    kbSearch/graphTraverse/translate actions and the Tier-3 path, none of which
    reach the SQL firewall. These assert on the DECISION, the same discipline as
    the SQL-path invariants above.
    """

    def test_grant_on_namespace_is_admitted(self):
        denied, _ = _admit([_grant(role=_CEDAR_OK_ROLE)])

        assert not denied, "a principal holding a query role on the namespace must be admitted"

    def test_no_grant_anywhere_is_denied(self):
        """No resolved roles → Cedar fails closed."""
        denied, _ = _admit([])

        assert denied, "an authenticated caller with no grant must not read the namespace"

    def test_grant_on_another_namespace_does_not_admit_this_one(self):
        """Cross-namespace read: a role on ns-OTHER must not admit this namespace."""
        denied, _ = _admit([{"role": _CEDAR_OK_ROLE, "resourceId": "ns-OTHER"}], namespace=_NS)

        assert denied, "a grant on another namespace must not admit this one"

    def test_evaluator_error_denies(self):
        """A Cedar evaluator failure must fail closed, never admit."""
        boom = MagicMock()
        boom.authorize.side_effect = RuntimeError("cedar exploded")
        result = SQLFirewall(cedar_authorizer=boom).authorize_namespace(_NS, {"userId": "u", "globalRoles": ["x"]})

        assert result.denied, "an evaluator error must deny (fail closed)"
        # The evaluator's own exception text is logged, not returned.
        assert result.reason == "Authorization policy evaluation failed"
        assert "cedar exploded" not in (result.reason or "")

    def test_deny_carries_reason_and_no_sql(self):
        """A policy deny returns the reason for logging, with no SQL on this path."""
        denying = MagicMock()
        denying.authorize.return_value = CedarDecision(allowed=False, reason="not permitted to query this namespace")
        result = SQLFirewall(cedar_authorizer=denying).authorize_namespace(_NS, {"userId": "u", "globalRoles": ["x"]})

        assert result.denied
        assert result.reason == "not permitted to query this namespace"
        assert result.authorized_sql == ""
