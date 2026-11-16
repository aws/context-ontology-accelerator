# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Playground's Principal label — email when known, sub otherwise.

``profile["userId"]`` is the JWT sub, a UUID that means nothing to the person
reading the rationale panel. ``display_principal`` prefers the email claim for
display while leaving ``userId`` as the key everything else uses.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from coa_serve.identity import display_principal
from coa_serve.role_resolver import ResolvedProfile

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[2] / "src" / "coa_serve"


class TestDisplayPrincipal:
    def test_prefers_email_over_sub(self):
        assert display_principal({"userId": "e4f1c2a0-uuid", "email": "alice@example.com"}) == "alice@example.com"

    def test_falls_back_to_sub_when_no_email(self):
        """Every IdP guarantees sub; email is optional, so the label must degrade."""
        assert display_principal({"userId": "e4f1c2a0-uuid"}) == "e4f1c2a0-uuid"

    def test_empty_email_falls_back_to_sub(self):
        """TokenClaims.email defaults to "" — an empty string must not win."""
        assert display_principal({"userId": "e4f1c2a0-uuid", "email": ""}) == "e4f1c2a0-uuid"

    def test_tolerates_none_profile(self):
        """Tier 3 passes an optional profile; the guard lives here, not per call site."""
        assert display_principal(None) is None

    def test_tolerates_empty_profile(self):
        assert display_principal({}) is None


class TestEmailReachesTheProfile:
    """The label is only useful if the resolver actually injects the claim."""

    def test_inject_into_writes_the_jwt_email(self):
        profile: dict = {}
        ResolvedProfile(user_id="sub-123", email="alice@example.com").inject_into(profile)

        assert profile["email"] == "alice@example.com"
        assert profile["userId"] == "sub-123", "sub stays the authorization key"
        assert display_principal(profile) == "alice@example.com"

    def test_absent_email_is_not_written(self):
        """A token with no email claim must not leave an empty key behind."""
        profile: dict = {}
        ResolvedProfile(user_id="sub-123").inject_into(profile)

        assert "email" not in profile
        assert display_principal(profile) == "sub-123"

    def test_injected_email_overwrites_a_client_supplied_one(self):
        """main.py strips client keys, but injection must win regardless.

        The profile arrives from the request body on the no-JWT path, so if a
        caller-supplied email ever survived, the panel would render an identity
        the token never asserted.
        """
        profile = {"email": "attacker@evil.example"}
        ResolvedProfile(user_id="sub-123", email="alice@example.com").inject_into(profile)

        assert profile["email"] == "alice@example.com"


class TestNoRawSubReachesTheUI:
    """Drift guard: walks the AST of every serve module and fails on a ``principal``
    value that is not a ``display_principal(...)`` call."""

    _ALLOWED_NAMES = {"principal_label", "principal_id"}

    def _principal_values(self, tree: ast.AST):
        """Yield (lineno, node) for every ``principal=`` kwarg and ``"principal":`` value."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "principal":
                        yield kw.value.lineno, kw.value
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=False):
                    if isinstance(key, ast.Constant) and key.value == "principal":
                        yield value.lineno, value

    def test_no_user_facing_principal_is_a_raw_profile_lookup(self):
        offenders: list[str] = []

        for path in sorted(_SRC.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for lineno, value in self._principal_values(tree):
                # profile.get("userId") / (profile or {}).get("userId")
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "get":
                    arg = value.args[0] if value.args else None
                    if isinstance(arg, ast.Constant) and arg.value == "userId":
                        offenders.append(f"{path.relative_to(_SRC)}:{lineno} — raw sub lookup")

        assert offenders == [], "user-facing principal must go through display_principal:\n  " + "\n  ".join(offenders)

    def test_every_response_metadata_principal_uses_the_helper(self):
        """The ``principal=`` kwarg on the assemblers is what the panel renders."""
        bad: list[str] = []

        for path in sorted(_SRC.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for lineno, value in self._principal_values(tree):
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "display_principal"
                ):
                    continue
                if isinstance(value, ast.Name) and value.id in self._ALLOWED_NAMES:
                    continue
                bad.append(f"{path.relative_to(_SRC)}:{lineno} — {ast.dump(value)[:60]}")

        assert bad == [], (
            "every principal value must be display_principal(...) or a local "
            "already derived from it:\n  " + "\n  ".join(bad)
        )
