# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for constants.dialect_hint / invalid_dialect_message."""

from __future__ import annotations

from coa_metrics.constants import VALID_SQL_DIALECTS, dialect_hint, invalid_dialect_message


class TestDialectHint:
    def test_athena_gets_trino_hint(self):
        """ATHENA is not a valid SqlDialect (and per team decision
        will NOT be added as one — Athena's engine roadmap may drop Trino).
        The hint must point the caller at TRINO instead."""
        hint = dialect_hint("ATHENA")
        assert hint is not None
        assert "TRINO" in hint
        assert "Athena-backed sources" in hint

    def test_athena_hint_is_case_insensitive(self):
        assert dialect_hint("athena") is not None

    def test_unknown_dialect_has_no_hint(self):
        """A typo or genuinely unsupported dialect has no fabricated hint."""
        assert dialect_hint("ORACLE") is None


class TestInvalidDialectMessage:
    def test_athena_gets_trino_hint(self):
        msg = invalid_dialect_message("ATHENA")
        assert "ATHENA" in msg
        assert "TRINO" in msg

    def test_unknown_dialect_has_no_hint(self):
        msg = invalid_dialect_message("ORACLE")
        assert "ORACLE" in msg
        # Assert on the absence of the HINT, not of the word "TRINO": the base
        # message always lists every valid dialect (see
        # test_base_message_lists_valid_dialects), TRINO among them, so
        # `"TRINO" not in msg` can never hold and contradicted that test.
        assert dialect_hint("ORACLE") is None
        assert "use TRINO" not in msg
        assert "(" not in msg, f"expected no hint suffix, got: {msg}"

    def test_base_message_lists_valid_dialects(self):
        msg = invalid_dialect_message("ATHENA")
        for dialect in VALID_SQL_DIALECTS:
            assert dialect in msg
