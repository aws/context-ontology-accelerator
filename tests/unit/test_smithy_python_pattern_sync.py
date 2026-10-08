# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Smithy @pattern traits and their hand-written Python twins must agree.

Some request paths do not go through the Smithy-generated models (grant
handlers parse the body by hand, the induction API uses its own FastAPI
models, OSI import builds metrics directly), so the same allowlist lives in
Python too. These tests read the pattern out of the .smithy file and check
both regexes accept and reject the same corpus, so one cannot be loosened
without the other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from coa_common.constants import ONTOLOGY_URI_PREFIX_RE, PRINCIPAL_ID_RE

pytestmark = pytest.mark.unit

_SMITHY_DIR = Path(__file__).resolve().parents[2] / "models" / "src" / "main" / "smithy"


def _smithy_pattern(filename: str, shape: str) -> re.Pattern[str]:
    """Return the compiled @pattern of ``string <shape>`` in *filename*."""
    text = (_SMITHY_DIR / filename).read_text()
    match = re.search(rf'@pattern\("((?:[^"\\]|\\.)*)"\)\s*(?:@\w+\([^)]*\)\s*)*string {shape}\b', text)
    assert match, f"no @pattern found for {shape} in {filename}"
    # Undo Smithy string escaping (``\\`` -> ``\``) to get the regex source.
    return re.compile(match.group(1).encode().decode("unicode_escape"))


_PRINCIPAL_IDS = [
    "alice@example.com",
    "first.last+tag@example.co.uk",
    "550e8400-e29b-41d4-a716-446655440000",
    "Data Engineers",
    "agent:orders-bot/v2",
    "o'brien@example.com",
    "R&D Team",
    "Sales (EMEA)",
    "Ventes-Été",
    "<a onclick=prompt(1);>ClickMe</a>",
    'alice"quote',
    "alice`quote",
    "back\\slash",
    "Namespace::x",
    "a#b",
    "a|b",
    "tab\tchar",
    "",
]

_URI_PREFIXES = [
    "http://x/o#",
    "https://example.com/onto",
    "https://ontology.example.com:8443/sales/v1_2/~team/%20x#",
    "http://<script>alert(1)</script>",
    'https://example.com/a"x',
    "https://example.com/a b",
    "https://user@example.com/o#",
    "https://example.com/o?x=1",
    "javascript:alert(1)",
    "https://example.com:65535/o#",
    "https://example.com:65536/o#",
    "https://example..com/o#",
    "https://-example.com/o#",
    "https://example.com///x#",
    "https://example.com/../x#",
    "https://example.com/a%20b#",
    "https://example.com/a%zz#",
]

_DATA_SOURCE_IDS = [
    "ds-1",
    "ds.abc_123",
    "550e8400-e29b-41d4-a716-446655440000",
    "<script>",
    "ds 1",
    "a/b",
    "SRC#x",
    "",
]


@pytest.mark.parametrize("value", _PRINCIPAL_IDS)
def test_principal_id_patterns_agree(value: str) -> None:
    smithy = _smithy_pattern("grant.smithy", "PrincipalId")
    assert bool(smithy.fullmatch(value)) == bool(PRINCIPAL_ID_RE.fullmatch(value))


@pytest.mark.parametrize("value", _URI_PREFIXES)
def test_ontology_uri_prefix_patterns_agree(value: str) -> None:
    smithy = _smithy_pattern("ontology-induction.smithy", "OntologyUriPrefix")
    assert bool(smithy.fullmatch(value)) == bool(ONTOLOGY_URI_PREFIX_RE.fullmatch(value))


@pytest.mark.parametrize("value", _DATA_SOURCE_IDS)
def test_data_source_id_patterns_agree(value: str) -> None:
    from coa_metrics.source_status import _DATA_SOURCE_ID_RE

    smithy = _smithy_pattern("metric-service.smithy", "DataSourceId")
    assert bool(smithy.fullmatch(value)) == bool(_DATA_SOURCE_ID_RE.fullmatch(value))
