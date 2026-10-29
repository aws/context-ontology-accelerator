# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The display-only metric ``label`` (#1259, contract #1295) on every write and read path.

Covers the value rules (API and OSI YAML alike), ValidateMetric/CreateMetric
agreement, storage on ``vocab#displayLabel``, the update full-replace, the OSI
round trip through the async worker's plan payload, and that the label never
reaches anything that matches on metrics (embedding text, aiContext JSON).
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from coa_metrics.api import contract
from coa_metrics.api.create_metric import _metric_to_dict
from coa_metrics.api.create_metric import handler as create_handler
from coa_metrics.api.export_osi import _metric_to_osi
from coa_metrics.api.import_job_store import MetricDisposition
from coa_metrics.api.import_osi import _osi_metric_to_definition
from coa_metrics.api.import_osi import handler as import_handler
from coa_metrics.api.import_plan_payload import canonical_plan_payload_bytes, parse_plan_payload
from coa_metrics.api.import_worker import _build_offset_plan, _overwrite_warning
from coa_metrics.api.update_metric import handler as update_handler
from coa_metrics.api.validate_metric import handler as validate_handler
from coa_metrics.neptune_client import (
    COA_VOCAB,
    RDFS,
    MetricAiContext,
    MetricDefinition,
    MetricDialect,
    MetricNeptuneClient,
)
from coa_metrics.opensearch_client import _build_embedding_text
from coa_metrics.osi_parser import OsiDocument, parse_osi_yaml, serialize_osi_yaml
from coa_metrics.source_status import PERMISSIVE_ENV, SourceValidationUnavailableError

pytestmark = pytest.mark.unit

# A marker inside rejected values: a response that contained it would be echoing the value.
_MARK = "ZQXmark"

# Exact messages. Model-level rules (Smithy @length/@pattern and the string
# type) reject the request as a whole; check_metric_label's rules come back
# from ValidateMetric as an ERROR finding.
_TYPE_MESSAGE = "label must be a string"
_LENGTH_MESSAGE = "label must be 1-256 characters"
_FORBIDDEN_MESSAGE = (
    "label must not contain control characters, line or paragraph separators, unpaired surrogates, "
    "noncharacters, bidi embeddings, overrides or isolates, invisible format characters, '<' or '>'"
)
_VISIBLE_MESSAGE = "label must contain at least one visible character"


def _forbidden(code_point: int) -> str:
    """The rejected-character message naming the first rejected code point."""
    return f"{_FORBIDDEN_MESSAGE} (found U+{code_point:04X})"


# id -> (rejected label, message). Each case names the rule it exercises.
# Rejected by model validation: CreateMetric and ValidateMetric both return 400.
REJECTED_BY_MODEL: dict[str, tuple[str, str]] = {
    # Pydantic reports a lone surrogate as string_unicode; it is the character rule, not the type.
    "lone-surrogate": (f"{_MARK}\ud800", _FORBIDDEN_MESSAGE),
    "less-than": (f"{_MARK}<b>", _forbidden(0x3C)),
    "greater-than": (f"{_MARK}>", _forbidden(0x3E)),
    "tab": (f"{_MARK}\tx", _forbidden(0x09)),
    "del": (f"{_MARK}\x7f", _forbidden(0x7F)),
    "empty": ("", _LENGTH_MESSAGE),
    "too-long": ("あ" * 257, _LENGTH_MESSAGE),
    "too-long-astral": ("\U00020bb7" * 257, _LENGTH_MESSAGE),
}

# Rejected by check_metric_label: CreateMetric returns 400, ValidateMetric 200 with an ERROR finding.
REJECTED_BY_CHECKER: dict[str, tuple[str, str]] = {
    "ideographic-space-only": ("\u3000", _VISIBLE_MESSAGE),
    "ascii-space-only": ("   ", _VISIBLE_MESSAGE),
    "bidi-override": (f"{_MARK}\u202e売上", _forbidden(0x202E)),
    "bidi-isolate": (f"{_MARK}\u2066売上\u2069", _forbidden(0x2066)),
    "noncharacter-fdd0": (f"{_MARK}\ufdd0", _forbidden(0xFDD0)),
    "noncharacter-plane-end": (f"{_MARK}\U0010ffff", _forbidden(0x10FFFF)),
    # The model's "$" lets a final "\n" through; the checker catches it.
    "trailing-newline": (f"{_MARK}\n", _forbidden(0x0A)),
    "c1-control": (f"{_MARK}\x85", _forbidden(0x85)),
    "line-separator": (f"{_MARK}\u2028x", _forbidden(0x2028)),
    "paragraph-separator": (f"{_MARK}\u2029x", _forbidden(0x2029)),
    "zero-width-space": (f"{_MARK}\u200bx", _forbidden(0x200B)),
    "word-joiner": (f"{_MARK}\u2060x", _forbidden(0x2060)),
    "bom": (f"{_MARK}\ufeff", _forbidden(0xFEFF)),
    "tag-character": (f"{_MARK}\U000e0041", _forbidden(0xE0041)),
    "soft-hyphen": (f"{_MARK}\u00adx", _forbidden(0x00AD)),
    "interlinear-annotation": (f"{_MARK}\ufff9hidden\ufffax\ufffb", _forbidden(0xFFF9)),
    "deprecated-format": (f"{_MARK}\u206ax", _forbidden(0x206A)),
    "shorthand-format": (f"\U0001bca0{_MARK}", _forbidden(0x1BCA0)),
    "combining-mark-only": ("\u0301\u0302", _VISIBLE_MESSAGE),
    "variation-selector-only": ("\ufe0f", _VISIBLE_MESSAGE),
    "hangul-filler": ("\u3164", _VISIBLE_MESSAGE),
    "halfwidth-hangul-filler": ("\uffa0", _VISIBLE_MESSAGE),
    "braille-blank": ("\u2800", _VISIBLE_MESSAGE),
    "lrm-only": ("\u200e", _VISIBLE_MESSAGE),
    "unassigned-only-bmp": ("\ufff0", _VISIBLE_MESSAGE),
    "unassigned-only-astral": ("\U000e0080", _VISIBLE_MESSAGE),
    # Cn in Python 3.12 (Unicode 15.0), Mn from Unicode 16.0: not visible either way.
    "u0897-only": ("\u0897", _VISIBLE_MESSAGE),
}

# id -> rejected label, every rule.
REJECTED: dict[str, str] = {key: value for key, (value, _) in (REJECTED_BY_MODEL | REJECTED_BY_CHECKER).items()}

# id -> accepted label.
ACCEPTED: dict[str, str] = {
    "japanese": "売上合計",
    "zwj": "👩\u200d💻 開発",
    "zwnj": "می\u200cخواهم",
    "lrm": "Revenue \u200eعائد",
    "rlm": "عائد\u200f Revenue",
    "alm": "\u061c١٢٣ عائد",
    "ideographic-variation-selector": "葛\U000e0100飾区",
    "inner-spaces": " 売上 合計 ",
    "equal-to-a-name": "revenue_total",
    "max-astral": "\U00020bb7" * 256,
    "max-bmp": "あ" * 256,
}


@pytest.fixture(autouse=True)
def _permissive_source_lookup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(PERMISSIVE_ENV, "true")
    yield


def _metric_body(label: Any = None, *, with_label: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "monthly_revenue",
        "description": "Total revenue from all orders in a calendar month",
        "expression": {
            "dialects": [{"dialect": "TRINO", "expression": "SELECT SUM(total_amount) AS r FROM orders"}],
        },
        "dataSourceId": "ds-abc123",
        "sourceTable": "orders",
        "aiContext": {"synonyms": ["total sales"], "instructions": "Use with order_date.", "examples": []},
    }
    if with_label:
        body["label"] = label
    return body


def _event(body: dict[str, Any], *, name: str | None = None) -> dict[str, Any]:
    path = {"namespaceId": "test-ns"}
    if name:
        path["name"] = name
    return {
        "httpMethod": "POST",
        "pathParameters": path,
        "body": json.dumps(body),
        "requestContext": {"authorizer": {"claims": {"email": "test@example.com"}}},
    }


def _definition(label: str | None = None, name: str = "monthly_revenue") -> MetricDefinition:
    return MetricDefinition(
        name=name,
        description="Total revenue",
        expression_dialects=[MetricDialect(dialect="TRINO", expression="SELECT SUM(x) AS r FROM orders")],
        data_source_id="ds-abc123",
        source_table="orders",
        ai_context=MetricAiContext(synonyms=["total sales"], instructions="Sum it.", examples=["Revenue?"]),
        defined_by="steward@example.com",
        effective_from="2026-10-01",
        label=label,
    )


def _create(body: dict[str, Any]) -> tuple[dict[str, Any], MagicMock]:
    with (
        patch("coa_metrics.api.create_metric._get_neptune") as neptune,
        patch("coa_metrics.api.create_metric._get_opensearch"),
        patch("coa_metrics.api.create_metric._get_eventbridge"),
    ):
        neptune.return_value.get_metric.return_value = None
        return create_handler(_event(body), None), neptune.return_value


def _validate(body: dict[str, Any]) -> dict[str, Any]:
    with patch.multiple(
        "coa_metrics.api.validate_metric",
        _get_data_source_lookup=MagicMock(return_value=None),
        _get_ontology_lookup=MagicMock(return_value=None),
        check_source_approved=MagicMock(return_value=None),
        check_source_table_exists=MagicMock(return_value=None),
    ):
        return validate_handler(_event(body), None)


def _label_findings(resp: dict[str, Any]) -> list[dict[str, str]]:
    return [w for w in json.loads(resp["body"]).get("warnings", []) if w["field"] == "label"]


def _yaml_scalar(value: str) -> str:
    """A YAML double-quoted scalar carrying ``value`` exactly, escapes for anything non-ASCII-printable."""
    out = []
    for char in value:
        if " " <= char <= "~" and char not in '"\\':
            out.append(char)
        else:
            out.append(f"\\U{ord(char):08x}")
    return '"' + "".join(out) + '"'


def _osi_yaml(label_line: str | None, *, top_level_label: bool = False) -> str:
    top = "    label: top-level-value\n" if top_level_label else ""
    label = f"          label: {label_line}\n" if label_line is not None else ""
    return (
        'osi_spec_version: "1.0"\n'
        "metrics:\n"
        "  - name: monthly_revenue\n"
        f"{top}"
        '    description: "Total revenue"\n'
        "    expression:\n"
        "      dialects:\n"
        "        - dialect: ANSI_SQL\n"
        '          expression: "SELECT SUM(total_amount) AS r FROM orders"\n'
        "    custom_extensions:\n"
        "      - vendor_name: COA\n"
        "        data:\n"
        "          data_source_id: ds-abc123\n"
        "          source_table: orders\n"
        f"{label}"
    )


def _import(content: str) -> tuple[dict[str, Any], MagicMock]:
    with (
        patch("coa_metrics.api.import_osi._get_neptune") as neptune,
        patch("coa_metrics.api.import_osi._get_opensearch"),
    ):
        neptune.return_value.get_metric.return_value = None
        event = {
            "httpMethod": "POST",
            "pathParameters": {"namespaceId": "test-ns"},
            "body": json.dumps({"content": content}),
            "requestContext": {"authorizer": {"email": "test@example.com"}},
        }
        return import_handler(event, None), neptune.return_value


# ── The value rules ─────────────────────────────────────────────────────


@pytest.mark.parametrize("value", REJECTED.values(), ids=REJECTED.keys())
def test_check_metric_label_rejects_without_echoing_the_value(value: str) -> None:
    message = contract.check_metric_label(value)
    assert message is not None
    assert message.startswith("label ")
    assert _MARK not in message


@pytest.mark.parametrize("value", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_check_metric_label_accepts(value: str) -> None:
    assert contract.check_metric_label(value) is None


@pytest.mark.parametrize("value", [True, 1, 1.5, [], ["a"], {"a": 1}])
def test_check_metric_label_never_converts_a_non_string(value: Any) -> None:
    assert contract.check_metric_label(value) == "label must be a string"


def test_check_metric_label_none_means_no_label() -> None:
    assert contract.check_metric_label(None) is None


def test_surrogate_pair_characters_count_as_one_code_point() -> None:
    label = "\U00020bb7" * 256
    assert len(label.encode("utf-16-le")) // 2 == 512
    assert contract.check_metric_label(label) is None


def test_length_is_counted_after_nfc_normalization() -> None:
    nfd = unicodedata.normalize("NFD", "が" * 256)
    assert len(nfd) == 512
    assert contract.check_metric_label(nfd) is None
    assert contract.normalize_metric_label(nfd) == "が" * 256


def test_length_counts_characters_that_nfc_lengthens() -> None:
    # U+0958 is a composition exclusion: NFC turns it into U+0915 U+093C, so a
    # label that is 200 code points as sent is 400 as stored (metrics.md says so).
    assert unicodedata.normalize("NFC", "\u0958") == "\u0915\u093c"
    assert contract.check_metric_label("\u0958" * 128) is None
    assert contract.check_metric_label("\u0958" * 129) == "label must be 1-256 characters"


# Every rejected format character, listed here independently of contract.py so
# that narrowing a range there fails this test.
_REJECTED_FORMAT_CODE_POINTS = [
    0x00AD,
    0x180E,
    0x200B,
    *range(0x202A, 0x202F),
    *range(0x2060, 0x2070),
    0xFEFF,
    *range(0xFFF9, 0xFFFC),
    *range(0x1BCA0, 0x1BCA4),
    *range(0x1D173, 0x1D17B),
    *range(0xE0000, 0xE0080),
]


@pytest.mark.parametrize("code_point", _REJECTED_FORMAT_CODE_POINTS, ids=lambda cp: f"U+{cp:04X}")
def test_every_rejected_format_character_is_rejected(code_point: int) -> None:
    assert contract.check_metric_label(f"売上{chr(code_point)}合計") == _forbidden(code_point)


@pytest.mark.parametrize("code_point", [0x200C, 0x200D, 0x200E, 0x200F, 0x061C], ids=lambda cp: f"U+{cp:04X}")
def test_allowed_format_characters_are_accepted(code_point: int) -> None:
    assert contract.check_metric_label(f"売上{chr(code_point)}合計") is None


# ── API: create, validate, update ───────────────────────────────────────


@pytest.mark.parametrize(("value", "message"), REJECTED_BY_MODEL.values(), ids=REJECTED_BY_MODEL.keys())
def test_model_level_label_rules_reject_create_and_validate_alike(value: str, message: str) -> None:
    created, neptune = _create(_metric_body(value))
    assert created["statusCode"] == 400
    assert json.loads(created["body"])["message"] == message
    assert _MARK not in created["body"]
    neptune.create_metric.assert_not_called()

    validated = _validate(_metric_body(value))
    assert validated["statusCode"] == 400
    assert json.loads(validated["body"])["message"] == message
    assert _MARK not in validated["body"]


@pytest.mark.parametrize(("value", "message"), REJECTED_BY_CHECKER.values(), ids=REJECTED_BY_CHECKER.keys())
def test_checker_label_rules_reject_create_and_report_in_validate(value: str, message: str) -> None:
    created, neptune = _create(_metric_body(value))
    assert created["statusCode"] == 400
    assert json.loads(created["body"])["message"] == message
    assert _MARK not in created["body"]
    neptune.create_metric.assert_not_called()

    validated = _validate(_metric_body(value))
    assert validated["statusCode"] == 200
    assert _label_findings(validated) == [{"field": "label", "message": message, "severity": "ERROR"}]
    assert _MARK not in validated["body"]


@pytest.mark.parametrize("value", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_create_and_validate_agree_on_accepted_labels(value: str) -> None:
    created, neptune = _create(_metric_body(value))
    assert created["statusCode"] == 201, created["body"]
    assert json.loads(created["body"])["metric"]["label"] == value
    assert neptune.create_metric.call_args.args[1].label == value

    validated = _validate(_metric_body(value))
    assert validated["statusCode"] == 200
    assert _label_findings(validated) == []


@pytest.mark.parametrize("value", [True, 7, [], {"a": "b"}])
def test_create_and_validate_reject_a_non_string_label(value: Any) -> None:
    created, _ = _create(_metric_body(value))
    validated = _validate(_metric_body(value))
    for resp in (created, validated):
        assert resp["statusCode"] == 400
        assert json.loads(resp["body"])["message"] == "label must be a string"


def test_validate_reports_a_bad_label_alongside_a_sql_error() -> None:
    body = _metric_body("売上\u202e")
    body["expression"] = {"dialects": [{"dialect": "TRINO", "expression": "COUNT(*)"}]}
    resp = _validate(body)
    assert resp["statusCode"] == 200
    warnings = json.loads(resp["body"])["warnings"]
    assert warnings[0]["field"] == "label"
    assert warnings[0]["severity"] == "ERROR"
    assert any(w["field"] != "label" and w["severity"] == "ERROR" for w in warnings)


@pytest.mark.parametrize("label", ["売上\u202e", None])
def test_validate_reports_a_bad_label_during_a_source_gate_outage(label: str | None) -> None:
    with patch.multiple(
        "coa_metrics.api.validate_metric",
        _get_data_source_lookup=MagicMock(return_value=None),
        _get_ontology_lookup=MagicMock(return_value=None),
        check_source_approved=MagicMock(side_effect=SourceValidationUnavailableError("catalog down")),
        check_source_table_exists=MagicMock(return_value=None),
    ):
        resp = validate_handler(_event(_metric_body(label, with_label=label is not None)), None)
    if label is None:
        # Nothing deterministic to report: the outage still means "try again later".
        assert resp["statusCode"] == 503
        return
    # Create rejects this label with a 400 before its source gate, so validate says the same.
    assert resp["statusCode"] == 200
    assert _label_findings(resp) == [{"field": "label", "message": _forbidden(0x202E), "severity": "ERROR"}]


def test_create_stores_the_nfc_form() -> None:
    created, neptune = _create(_metric_body(unicodedata.normalize("NFD", "ガス料金")))
    assert created["statusCode"] == 201
    assert neptune.create_metric.call_args.args[1].label == "ガス料金"
    assert json.loads(created["body"])["metric"]["label"] == "ガス料金"


@pytest.mark.parametrize("body", [_metric_body(with_label=False), _metric_body(None)], ids=["absent", "null"])
def test_create_without_label_omits_it(body: dict[str, Any]) -> None:
    created, neptune = _create(body)
    assert created["statusCode"] == 201
    assert "label" not in json.loads(created["body"])["metric"]
    assert neptune.create_metric.call_args.args[1].label is None


def _update(body: dict[str, Any], existing: MetricDefinition) -> tuple[dict[str, Any], MagicMock]:
    with (
        patch("coa_metrics.api.update_metric._get_neptune") as neptune,
        patch("coa_metrics.api.update_metric._get_opensearch"),
        patch("coa_metrics.api.update_metric._get_eventbridge"),
    ):
        neptune.return_value.get_metric.return_value = existing
        event = _event(body, name="monthly_revenue")
        event["httpMethod"] = "PUT"
        return update_handler(event, None), neptune.return_value


@pytest.mark.parametrize("body", [_metric_body(with_label=False), _metric_body(None)], ids=["absent", "null"])
def test_update_without_label_clears_it(body: dict[str, Any]) -> None:
    resp, neptune = _update(body, _definition("売上合計"))
    assert resp["statusCode"] == 200
    assert neptune.update_metric.call_args.args[2].label is None
    assert "label" not in json.loads(resp["body"])["metric"]


def test_update_that_sends_the_label_back_keeps_it() -> None:
    resp, neptune = _update(_metric_body("売上合計"), _definition("売上合計"))
    assert resp["statusCode"] == 200
    assert neptune.update_metric.call_args.args[2].label == "売上合計"
    assert json.loads(resp["body"])["metric"]["label"] == "売上合計"


@pytest.mark.parametrize("value", ["", f"{_MARK}\u202e", f"{_MARK}<"], ids=["empty", "bidi", "angle"])
def test_update_rejects_a_bad_label(value: str) -> None:
    resp, neptune = _update(_metric_body(value), _definition())
    assert resp["statusCode"] == 400
    assert "label" in json.loads(resp["body"])["message"]
    assert _MARK not in resp["body"]
    neptune.update_metric.assert_not_called()


# 400 code points as sent, 200 after NFC: within the limit only if the handler
# normalizes before the model counts it.
_LONG_NFD_LABEL = unicodedata.normalize("NFD", "が" * 200)
_LONG_NFC_LABEL = "が" * 200


@pytest.mark.parametrize("path", ["create", "update", "validate"])
def test_every_write_path_counts_the_limit_after_nfc_and_stores_nfc(path: str) -> None:
    assert len(_LONG_NFD_LABEL) == 400
    body = _metric_body(_LONG_NFD_LABEL)
    if path == "create":
        resp, neptune = _create(body)
        assert resp["statusCode"] == 201, resp["body"]
        assert neptune.create_metric.call_args.args[1].label == _LONG_NFC_LABEL
    elif path == "update":
        resp, neptune = _update(body, _definition("売上合計"))
        assert resp["statusCode"] == 200, resp["body"]
        assert neptune.update_metric.call_args.args[2].label == _LONG_NFC_LABEL
    else:
        resp = _validate(body)
        assert resp["statusCode"] == 200, resp["body"]
        assert _label_findings(resp) == []
        return
    assert json.loads(resp["body"])["metric"]["label"] == _LONG_NFC_LABEL


def test_metric_to_dict_includes_label_only_when_set() -> None:
    assert _metric_to_dict(_definition("売上合計"))["label"] == "売上合計"
    assert "label" not in _metric_to_dict(_definition())


# ── Storage ─────────────────────────────────────────────────────────────


def test_label_is_stored_on_display_label_never_on_rdfs_or_skos() -> None:
    triples = MetricNeptuneClient()._build_triples("test-ns", _definition("売上合計"))
    rdfs_labels = [t for t in triples if f"<{RDFS}label>" in t]
    assert rdfs_labels == [rdfs_labels[0]]
    assert '"monthly_revenue"' in rdfs_labels[0]
    display = [t for t in triples if f"<{COA_VOCAB}displayLabel>" in t]
    assert len(display) == 1
    # Untagged plain literal.
    assert display[0].endswith('"売上合計" .')
    assert not any("skos" in t for t in triples)
    ai_context = next(t for t in triples if f"<{COA_VOCAB}aiContext>" in t)
    assert "売上合計" not in ai_context


def test_no_display_label_triple_without_a_label() -> None:
    triples = MetricNeptuneClient()._build_triples("test-ns", _definition())
    assert not any("displayLabel" in t for t in triples)


def _binding(predicate: str, value: str, **extra: str) -> dict[str, Any]:
    return {"p": {"type": "uri", "value": predicate}, "o": {"type": "literal", "value": value, **extra}}


def _stored_bindings() -> list[dict[str, Any]]:
    return [
        _binding(RDFS + "label", "monthly_revenue"),
        _binding(RDFS + "comment", "Total revenue"),
        _binding(COA_VOCAB + "dataSourceId", "ds-abc123"),
        _binding(COA_VOCAB + "sourceTable", "orders"),
        _binding(COA_VOCAB + "expressionDialects", json.dumps([{"dialect": "TRINO", "expression": "SELECT 1"}])),
    ]


@pytest.mark.parametrize("tagged_first", [True, False])
def test_parse_metric_returns_the_untagged_label_when_a_tagged_one_is_present(tagged_first: bool) -> None:
    tagged = _binding(COA_VOCAB + "displayLabel", "Total sales", **{"xml:lang": "en"})
    untagged = _binding(COA_VOCAB + "displayLabel", "売上合計")
    pair = [tagged, untagged] if tagged_first else [untagged, tagged]
    metric = MetricNeptuneClient()._parse_metric("monthly_revenue", _stored_bindings() + pair)
    assert metric.label == "売上合計"


def test_parse_metric_ignores_a_tagged_only_label() -> None:
    tagged = _binding(COA_VOCAB + "displayLabel", "Total sales", **{"xml:lang": "en"})
    metric = MetricNeptuneClient()._parse_metric("monthly_revenue", [*_stored_bindings(), tagged])
    assert metric.label is None


def test_stored_metric_without_a_label_still_reads_back() -> None:
    metric = MetricNeptuneClient()._parse_metric("monthly_revenue", _stored_bindings())
    assert metric.label is None
    assert metric.name == "monthly_revenue"
    assert "label" not in _metric_to_dict(metric)


def test_label_is_not_in_the_embedding_text() -> None:
    text = _build_embedding_text(_definition(f"{_MARK}売上合計"))
    assert _MARK not in text
    assert "売上合計" not in text


# ── OSI import (YAML path) ──────────────────────────────────────────────


@pytest.mark.parametrize("value", REJECTED.values(), ids=REJECTED.keys())
def test_import_skips_a_metric_with_a_rejected_label(value: str) -> None:
    resp, neptune = _import(_osi_yaml(_yaml_scalar(value)))
    assert resp["statusCode"] == 200, resp["body"]
    body = json.loads(resp["body"])
    assert body["metricsCreated"] == 0
    assert any("skipped" in w and "label" in w for w in body["warnings"])
    assert _MARK not in resp["body"]
    neptune.create_metric.assert_not_called()


@pytest.mark.parametrize("value", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_import_stores_an_accepted_label(value: str) -> None:
    resp, neptune = _import(_osi_yaml(_yaml_scalar(value)))
    assert json.loads(resp["body"])["metricsCreated"] == 1, resp["body"]
    assert neptune.create_metric.call_args.args[1].label == value


@pytest.mark.parametrize(
    ("yaml_value", "expected"),
    [
        ("yes", "label must be a string"),
        ("[]", "label must be a string"),
        ("[a, b]", "label must be a string"),
        ("12", "label must be a string"),
        ('""', "label must be 1-256 characters"),
    ],
)
def test_import_never_converts_a_yaml_label(yaml_value: str, expected: str) -> None:
    resp, neptune = _import(_osi_yaml(yaml_value))
    warnings = json.loads(resp["body"])["warnings"]
    assert any(w.endswith(f"skipped — {expected}") for w in warnings), warnings
    neptune.create_metric.assert_not_called()


@pytest.mark.parametrize("yaml_value", ["null", "~", None], ids=["null", "tilde", "absent"])
def test_import_null_or_missing_label_means_no_label(yaml_value: str | None) -> None:
    resp, neptune = _import(_osi_yaml(yaml_value))
    assert json.loads(resp["body"])["metricsCreated"] == 1
    assert neptune.create_metric.call_args.args[1].label is None


def test_import_stores_the_nfc_form() -> None:
    resp, neptune = _import(_osi_yaml(_yaml_scalar(unicodedata.normalize("NFD", "ガス料金"))))
    assert json.loads(resp["body"])["metricsCreated"] == 1
    assert neptune.create_metric.call_args.args[1].label == "ガス料金"


def test_top_level_label_is_ignored_with_a_warning() -> None:
    result = parse_osi_yaml(_osi_yaml(None, top_level_label=True))
    assert result.success
    assert result.document is not None
    assert result.document.metrics[0].custom_extensions is not None
    assert result.document.metrics[0].custom_extensions.label is None
    [warning] = [w for w in result.warnings if w.path.endswith(".label")]
    assert "custom_extensions" in warning.message
    assert "top-level-value" not in warning.message


def test_import_overwrite_warning_says_the_label_is_cleared_without_quoting_it() -> None:
    with (
        patch("coa_metrics.api.import_osi._get_neptune") as neptune,
        patch("coa_metrics.api.import_osi._get_opensearch"),
    ):
        neptune.return_value.get_metric.return_value = _definition(f"{_MARK}売上")
        event = {
            "httpMethod": "POST",
            "pathParameters": {"namespaceId": "test-ns"},
            "body": json.dumps({"content": _osi_yaml(None)}),
            "requestContext": {"authorizer": {"email": "test@example.com"}},
        }
        resp = import_handler(event, None)
    [warning] = json.loads(resp["body"])["warnings"]
    assert "label was cleared" in warning
    assert _MARK not in resp["body"]


def test_worker_overwrite_warning_mentions_a_cleared_label_only_when_cleared() -> None:
    existing = _definition(f"{_MARK}売上")
    cleared = _overwrite_warning("monthly_revenue", existing, _definition())
    kept = _overwrite_warning("monthly_revenue", existing, _definition("売上"))
    assert "label was cleared" in cleared
    assert _MARK not in cleared
    assert "label" not in kept


# ── OSI round trip, through the async worker's plan payload ─────────────


def _round_trip_document(metric: MetricDefinition) -> OsiDocument:
    yaml_text = serialize_osi_yaml(OsiDocument(metrics=[_metric_to_osi(metric)]))
    result = parse_osi_yaml(yaml_text)
    assert result.success, result.errors
    assert result.document is not None
    return result.document


def test_export_writes_label_in_the_coa_extension() -> None:
    yaml_text = serialize_osi_yaml(OsiDocument(metrics=[_metric_to_osi(_definition("売上合計"))]))
    assert "label: 売上合計" in yaml_text
    exported = parse_osi_yaml(yaml_text).document
    assert exported is not None
    assert exported.metrics[0].custom_extensions is not None
    assert exported.metrics[0].custom_extensions.label == "売上合計"


def test_export_leaves_label_out_when_there_is_none() -> None:
    yaml_text = serialize_osi_yaml(OsiDocument(metrics=[_metric_to_osi(_definition())]))
    assert "label" not in yaml_text


@pytest.mark.parametrize("label", ["売上合計", "\U00020bb7野家 \u200d", None])
def test_export_then_async_import_keeps_the_label(label: str | None) -> None:
    document = _round_trip_document(_definition(label))
    neptune = MagicMock()
    neptune.get_metric.return_value = None

    plan = _build_offset_plan("test-ns", 0, list(document.metrics), document, neptune)
    replayed = parse_plan_payload(canonical_plan_payload_bytes(plan))

    [entry] = replayed.entries
    assert entry.disposition == MetricDisposition.CREATE
    assert entry.definition is not None
    assert entry.definition.label == label
    # The sync converter agrees with the worker.
    assert _osi_metric_to_definition(document.metrics[0], document, "t").label == label


def test_async_plan_records_a_bad_label_as_that_metrics_error() -> None:
    result = parse_osi_yaml(_osi_yaml("yes"))
    assert result.document is not None
    plan = _build_offset_plan("test-ns", 0, list(result.document.metrics), result.document, MagicMock())
    [entry] = plan.entries
    assert entry.disposition == MetricDisposition.ERROR
    assert entry.error is not None
    assert entry.error.endswith("label must be a string")


def _duplicate_yaml(first_label: str | None, second_label: str | None) -> str:
    """Two definitions of the same metric in one file, the second replacing the first."""
    return _osi_yaml(first_label) + _osi_yaml(second_label).split("metrics:\n", 1)[1]


@pytest.mark.parametrize(
    ("first", "second", "cleared"),
    [(f'"{_MARK}売上"', None, True), (f'"{_MARK}売上"', '"売上"', False), (None, None, False)],
    ids=["label-then-none", "label-then-label", "none-then-none"],
)
def test_async_plan_duplicate_warning_says_when_the_label_is_cleared(
    first: str | None, second: str | None, cleared: bool
) -> None:
    result = parse_osi_yaml(_duplicate_yaml(first, second))
    assert result.document is not None
    neptune = MagicMock()
    neptune.get_metric.return_value = None
    plan = _build_offset_plan("test-ns", 0, list(result.document.metrics), result.document, neptune)
    created, duplicate = plan.entries
    assert created.disposition == MetricDisposition.CREATE
    assert duplicate.disposition == MetricDisposition.UPDATE
    assert duplicate.warning is not None
    assert duplicate.warning.startswith("Metric 'monthly_revenue' overwritten by a later definition in the same import")
    assert ("label was cleared" in duplicate.warning) is cleared
    assert _MARK not in duplicate.warning
