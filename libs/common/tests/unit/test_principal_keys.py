# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the shared principal-key sanitizer.

This helper is the single source of truth for encoding principal identifiers
into DynamoDB key components. Every writer (grant handlers) and reader (API
authorizer, serve-layer role resolver) depends on it producing identical
output, so these tests pin the exact encoding contract.
"""

from __future__ import annotations

import pytest
from coa_common import build_principal_keys, sanitize_principal_key


@pytest.mark.unit
class TestSanitizePrincipalKey:
    def test_plain_email_is_unchanged(self):
        assert sanitize_principal_key("alice@example.com") == "alice@example.com"

    def test_at_and_dot_are_preserved(self):
        # ``@`` and ``.`` are kept raw so emails stay human-readable in keys.
        assert sanitize_principal_key("first.last@sub.example.com") == "first.last@sub.example.com"

    def test_plus_is_encoded(self):
        # The case: ``+`` must be percent-encoded.
        assert sanitize_principal_key("foo+123@amazon.com") == "foo%2B123@amazon.com"

    def test_hash_is_encoded(self):
        # ``#`` is the key delimiter and must never survive unencoded.
        assert sanitize_principal_key("user#tag") == "user%23tag"
        assert sanitize_principal_key("a#b#c") == "a%23b%23c"

    def test_slash_is_encoded(self):
        assert sanitize_principal_key("group/eng") == "group%2Feng"

    def test_no_collision_between_literal_and_encoded_input(self):
        # A literal ``%23`` must not collide with an encoded ``#``.
        assert sanitize_principal_key("user%23name") != sanitize_principal_key("user#name")

    def test_mixed_case_email_is_lowercased(self):
        """Emails have case-insensitive semantics in every IdP we support, but
        the ``email`` claim on a JWT and the string an admin types into the
        UI grant form aren't guaranteed to match case. Without normalization,
        ``User::Alice@Example.com`` and ``User::alice@example.com`` would be
        two DIFFERENT rows in the RRM table — a silent-miss bug.
        """
        assert sanitize_principal_key("Alice@Example.com") == "alice@example.com"
        assert sanitize_principal_key("ALICE@EXAMPLE.COM") == "alice@example.com"
        # Whitespace at the edges is also stripped so a copy-paste artifact
        # doesn't split a grant onto two rows.
        assert sanitize_principal_key("  alice@example.com  ") == "alice@example.com"

    def test_mixed_case_email_lowercase_is_stable_after_special_chars(self):
        """Lowercasing runs before URL-encoding, so the ``+`` still encodes
        and the encoded form matches regardless of input case."""
        assert sanitize_principal_key("Foo+123@Amazon.com") == "foo%2B123@amazon.com"
        assert sanitize_principal_key("foo+123@amazon.com") == "foo%2B123@amazon.com"

    def test_non_email_identifier_is_not_lowercased(self):
        """A Cognito ``sub`` is opaque and case-sensitive — case-normalizing it
        would corrupt the identifier. Group names can also be legitimately
        mixed-case (e.g. an IdP group named ``Admin``). Only identifiers
        containing ``@`` — treated as emails by convention — get lowercased.
        """
        # sub UUID (contains no @)
        sub = "5438C418-C071-7067-5CF7-6A14FAAC7145"
        assert sanitize_principal_key(sub) == sub
        # Group names with mixed case survive.
        assert sanitize_principal_key("Admin") == "Admin"
        assert sanitize_principal_key("PlatformAdmin") == "PlatformAdmin"


@pytest.mark.unit
class TestBuildPrincipalKeys:
    """The shape of the PrincipalIndex GSI query list — every reader/writer
    of the ResourceRoleMappings table depends on this contract."""

    def test_sub_only_produces_one_user_key(self):
        keys = build_principal_keys(user_id="uuid-1234", email="", groups=[])
        assert keys == ["User::uuid-1234"]

    def test_email_only_produces_one_user_key(self):
        keys = build_principal_keys(user_id="", email="alice@example.com", groups=[])
        assert keys == ["User::alice@example.com"]

    def test_sub_and_email_produce_both_user_keys(self):
        keys = build_principal_keys(user_id="uuid-1234", email="alice@example.com", groups=[])
        assert keys == ["User::uuid-1234", "User::alice@example.com"]

    def test_sub_equal_to_email_deduplicates(self):
        # Upstream may fall back to email as user_id when sub is unavailable —
        # a duplicate query would double the DDB cost for no gain.
        keys = build_principal_keys(
            user_id="alice@example.com",
            email="alice@example.com",
            groups=[],
        )
        assert keys == ["User::alice@example.com"]

    def test_sub_equal_to_email_deduplicates_across_case(self):
        """When a JWT presents the email as ``sub`` in one case and the
        ``email`` claim in another (some IdPs do this), the reader should
        still issue ONE DDB query, not two — the lowercased form dedupes."""
        keys = build_principal_keys(
            user_id="Alice@Example.com",
            email="alice@example.com",
            groups=[],
        )
        assert keys == ["User::alice@example.com"]

    def test_groups_are_appended_after_users(self):
        keys = build_principal_keys(user_id="uuid-1234", email="alice@example.com", groups=["Admin", "eng"])
        assert keys == [
            "User::uuid-1234",
            "User::alice@example.com",
            "Group::Admin",
            "Group::eng",
        ]

    def test_group_names_are_sanitized(self):
        keys = build_principal_keys(user_id="", email="", groups=["group/eng"])
        assert keys == ["Group::group%2Feng"]

    def test_email_with_plus_is_sanitized(self):
        # The bug that motivated centralizing this: ``+`` must be encoded so
        # the reader's query matches the writer's stored key.
        keys = build_principal_keys(user_id="", email="foo+123@amazon.com", groups=[])
        assert keys == ["User::foo%2B123@amazon.com"]

    def test_email_with_mixed_case_and_plus_is_normalized(self):
        """Combines both normalization rules: lowercase the email, then encode
        the ``+``. Pins that both writer and reader converge on the same key
        no matter what case the admin used when granting."""
        keys = build_principal_keys(user_id="", email="Foo+123@Amazon.com", groups=[])
        assert keys == ["User::foo%2B123@amazon.com"]

    def test_special_chars_encoded_in_sub(self):
        """A ``sub`` containing ``+`` (rare but possible) is still URL-encoded,
        even though sub does NOT get lowercased. Ported up from the old
        context-manager test suite as part of consolidating to this file."""
        keys = build_principal_keys(user_id="uuid+1", email="", groups=[])
        assert keys == ["User::uuid%2B1"]

    def test_empty_inputs_produce_empty_list(self):
        assert build_principal_keys(user_id="", email="", groups=[]) == []
