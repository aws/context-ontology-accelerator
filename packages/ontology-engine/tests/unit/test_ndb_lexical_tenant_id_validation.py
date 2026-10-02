# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sink-side tenant_id validation for ``NeptuneDatabaseLexicalStore``.

``_label()`` interpolates ``self._tenant_id`` into a backtick-quoted
openCypher label, so any non-hex character that reaches the constructor is
an injection risk. The primary guard lives at ``to_graphrag_tenant_id``
(``libs/common/src/coa_common/constants.py``); this file exercises the
belt-to-that-suspenders check in the store's ``__init__`` — the sink must
fail closed even when a caller bypasses the derivation helper and hands the
tenant id in directly.
"""

from __future__ import annotations

import pytest
from coa_ontology.inducer.unstructured.stores.ndb_lexical import (
    NeptuneDatabaseLexicalStore,
)

pytestmark = pytest.mark.unit


class TestTenantIdValidation:
    """Verify ``NeptuneDatabaseLexicalStore.__init__`` rejects non-hex tenant IDs."""

    def test_accepts_empty_tenant_id(self):
        """Empty is a legitimate 'no scoping' signal."""
        store = NeptuneDatabaseLexicalStore(endpoint="neptune.example.com", tenant_id="")
        assert store._tenant_id == ""

    def test_accepts_hex_tenant_id(self):
        """A normally-derived tenant_id (hex-only) is accepted."""
        store = NeptuneDatabaseLexicalStore(
            endpoint="neptune.example.com",
            tenant_id="550e8400e29b41d4a71644665",
        )
        assert store._tenant_id == "550e8400e29b41d4a71644665"

    def test_accepts_uppercase_hex(self):
        """Uppercase hex is accepted (case-insensitive regex)."""
        store = NeptuneDatabaseLexicalStore(
            endpoint="neptune.example.com",
            tenant_id="ABCDEF0123456789",
        )
        assert store._tenant_id == "ABCDEF0123456789"

    @pytest.mark.parametrize(
        "hostile_tenant_id",
        [
            "550e8400`e29b41d4a71644665",  # backtick — breaks out of `__X__<tenant>__`
            "550e8400e29b41d4a71644665; DROP",  # semicolon
            "550e8400e29b41d4a71644665 UNION",  # whitespace + clause
            "550e8400e29b41d4a71644665'--",  # quote + comment
            "550e8400\\`e29b41d4a71644665",  # escaped backtick
            "550e8400.e29b41d4a71644665",  # dot — not hex
            "550e8400-e29b41d4a71644665",  # dash — not hex (post-strip stage should have removed)
            "not_hex_at_all",  # underscore + letters
        ],
    )
    def test_rejects_non_hex_tenant_id(self, hostile_tenant_id: str):
        """Anything that could break out of the ``_label`` backtick-quoted
        identifier at construction is rejected before the store is usable.

        Regression for the openCypher-injection class of bug where a caller
        that bypassed ``to_graphrag_tenant_id`` (or a future field addition
        the derivation helper doesn't cover) could reach the sink with
        cypher metacharacters intact.
        """
        with pytest.raises(ValueError, match="hex-only"):
            NeptuneDatabaseLexicalStore(
                endpoint="neptune.example.com",
                tenant_id=hostile_tenant_id,
            )
