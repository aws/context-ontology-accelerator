# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Detect SQL that filters on a value the primary guardrail anonymized.

The primary guardrail anonymizes PII entities in the question
(``infra/lib/stacks/foundation/guardrail-stack.ts``, ``PII_ENTITIES_CONFIG``), so a
name in "royalties for Saltmarsh Requiem" reaches the SQL writer as ``{NAME}``.
SQL comparing against that placeholder answers a different question than the one
asked, and an aggregate over no rows still returns a confident number.
"""

from __future__ import annotations

import re

import sqlglot
import structlog
from sqlglot.errors import TokenError
from sqlglot.tokens import TokenType

GUARDRAIL_PII_PLACEHOLDERS: tuple[str, ...] = (
    "{NAME}",
    "{EMAIL}",
    "{PHONE}",
    "{US_SOCIAL_SECURITY_NUMBER}",
    "{CREDIT_DEBIT_CARD_NUMBER}",
)

# The dialect the SQL firewall reads Tier-2 SQL as (``SQLFirewall.evaluate``), not
# the engine dialect it is transpiled to later: SQL is gated as it is authorized.
_READ_DIALECT = "trino"
_STRING_TOKENS = frozenset({TokenType.STRING, TokenType.NATIONAL_STRING, TokenType.UNICODE_STRING})
_SQL_STRING_LITERAL = re.compile(r"'((?:[^']|'')*)'")

logger = structlog.get_logger(__name__)


def _string_literals(sql: str) -> list[str]:
    """String literals as the firewall's tokenizer reads them (comments and quoted identifiers excluded)."""
    try:
        return [token.text for token in sqlglot.tokenize(sql, read=_READ_DIALECT) if token.token_type in _STRING_TOKENS]
    except TokenError:
        # The firewall rejects SQL it can't tokenize, so this only decides which reason the trace records.
        logger.info("guardrail_placeholder_tokenize_fallback")
        return _SQL_STRING_LITERAL.findall(sql)


def masked_literal_in_sql(sql: str) -> bool:
    """True if a string literal in ``sql`` contains a guardrail placeholder."""
    return any(
        placeholder in literal for literal in _string_literals(sql) for placeholder in GUARDRAIL_PII_PLACEHOLDERS
    )
