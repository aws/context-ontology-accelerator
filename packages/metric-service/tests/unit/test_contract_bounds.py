# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The bounds the metric-service handlers enforce by hand must equal ``metric-service.smithy``.

CreateMetric / UpdateMetric / ValidateMetric bodies go through their generated
Pydantic models, which carry the model's constraints. Everything else (path
labels, query parameters, the raw-JSON bodies of ImportOsi, GetOsiUploadUrl and
BulkDeleteMetrics) is checked by :mod:`coa_metrics.api.contract`.

Two halves:

* **Parity** — parses the Smithy model (the source of truth) and asserts every
  constant in :mod:`coa_metrics.api.contract` matches its ``@range`` /
  ``@length`` / ``@pattern``. Edit one without the other and this goes red.
* **Enforcement** — an out-of-contract value is rejected with a 400 that does
  not echo the value, and nothing reaches the backend.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from coa_metrics.api import contract
from coa_metrics.api.metric_api_handler import handler as metric_api_handler

pytestmark = pytest.mark.unit

# tests/unit/<this> -> up 4 = repo root
_SMITHY_DIR = Path(__file__).resolve().parents[4] / "models" / "src" / "main" / "smithy"
_NS = "550e8400-e29b-41d4-a716-446655440000"
_POISON = "<script>alert(1)</script>"


# ── Minimal Smithy reader ────────────────────────────────────────────────────


def _model() -> str:
    paths = [_SMITHY_DIR / "metric-service.smithy", _SMITHY_DIR / "common.smithy"]
    for path in paths:
        assert path.is_file(), f"{path.name} not found at {path}"
    return "\n".join(path.read_text() for path in paths)


def _block_members(text: str, start: int) -> dict[str, tuple[str, list[str]]]:
    """Members of the ``{ ... }`` block opening just before ``start``: ``name -> (target, traits)``."""
    members: dict[str, tuple[str, list[str]]] = {}
    traits: list[str] = []
    depth = 1
    for line in text[start:].splitlines():
        stripped = line.strip()
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0:
            break
        if stripped.startswith("@"):
            traits.append(stripped)
        elif m := re.match(r"([A-Za-z_]\w*)\s*:\s*([A-Za-z_][\w#]*)", stripped):
            members[m.group(1)] = (m.group(2), traits)
            traits = []
    return members


def _input_members(text: str, operation: str) -> dict[str, tuple[str, list[str]]]:
    op = re.search(rf"operation\s+{operation}\s*\{{", text)
    assert op, f"operation {operation} not found"
    inp = re.search(r"input\s*:=\s*(?:with\s*\[[^\]]*\]\s*)?\{", text[op.end() :])
    assert inp, f"{operation} input block not found"
    return _block_members(text, op.end() + inp.end())


def _structure_members(text: str, shape: str) -> dict[str, tuple[str, list[str]]]:
    m = re.search(rf"^structure\s+{shape}\b[^{{]*\{{", text, re.MULTILINE)
    assert m, f"structure {shape} not found"
    return _block_members(text, m.end())


def _shape_traits(text: str, shape: str) -> list[str]:
    """Trait lines applied to a top-level shape definition (doc comments skipped)."""
    m = re.search(rf"^(?:string|integer|list)\s+{shape}\b", text, re.MULTILINE)
    assert m, f"shape {shape} not found"
    traits: list[str] = []
    for line in reversed(text[: m.start()].splitlines()):
        stripped = line.strip()
        if stripped.startswith("@"):
            traits.append(stripped)
        elif not stripped.startswith("///"):
            break
    return traits


def _bound(traits: list[str], trait: str) -> tuple[int | None, int | None]:
    for t in traits:
        if t.startswith(f"@{trait}("):
            lo = re.search(r"min:\s*(\d+)", t)
            hi = re.search(r"max:\s*(\d+)", t)
            return (int(lo.group(1)) if lo else None, int(hi.group(1)) if hi else None)
    return (None, None)


def _resolved_bound(text: str, member: tuple[str, list[str]], trait: str) -> tuple[int | None, int | None]:
    """A member's bound: on the member itself, else on its target shape."""
    target, traits = member
    bound = _bound(traits, trait)
    if bound == (None, None) and target not in {"String", "Integer", "Boolean"}:
        bound = _bound(_shape_traits(text, target), trait)
    return bound


def _pattern(text: str, shape: str) -> str:
    for t in _shape_traits(text, shape):
        if m := re.match(r'@pattern\("(.*)"\)$', t):
            return m.group(1).encode().decode("unicode_escape")
    raise AssertionError(f"{shape} has no @pattern")


# ── Parity ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("operation", "member", "trait", "expected"),
    [
        ("GetMetric", "name", "length", (contract.METRIC_NAME_MIN_LENGTH, contract.METRIC_NAME_MAX_LENGTH)),
        ("UpdateMetric", "name", "length", (contract.METRIC_NAME_MIN_LENGTH, contract.METRIC_NAME_MAX_LENGTH)),
        ("DeleteMetric", "name", "length", (contract.METRIC_NAME_MIN_LENGTH, contract.METRIC_NAME_MAX_LENGTH)),
        (
            "ListMetrics",
            "maxResults",
            "range",
            (contract.LIST_METRICS_MAX_RESULTS_MIN, contract.LIST_METRICS_MAX_RESULTS_MAX),
        ),
        ("ListMetrics", "nextToken", "length", (1, contract.PAGINATION_TOKEN_MAX_LENGTH)),
        ("ListMetrics", "dataSourceId", "length", (1, contract.DATA_SOURCE_ID_MAX_LENGTH)),
        ("ListMetrics", "sourceTable", "length", (contract.SOURCE_TABLE_MIN_LENGTH, contract.SOURCE_TABLE_MAX_LENGTH)),
        ("GetImportJob", "jobId", "length", (contract.JOB_ID_MIN_LENGTH, contract.JOB_ID_MAX_LENGTH)),
        ("GetOsiUploadUrl", "filename", "length", (contract.FILENAME_MIN_LENGTH, contract.FILENAME_MAX_LENGTH)),
        ("ImportOsi", "content", "length", (None, contract.IMPORT_CONTENT_MAX_LENGTH)),
        ("ImportOsi", "s3Key", "length", (contract.IMPORT_S3_KEY_MIN_LENGTH, contract.IMPORT_S3_KEY_MAX_LENGTH)),
        ("BulkDeleteMetrics", "names", "length", (None, contract.BULK_DELETE_NAMES_MAX_ITEMS)),
        ("ExportOsi", "names", "length", (None, contract.EXPORT_NAMES_MAX_ITEMS)),
    ],
)
def test_input_bound_matches_model(operation: str, member: str, trait: str, expected: tuple) -> None:
    text = _model()
    declared = _resolved_bound(text, _input_members(text, operation)[member], trait)
    assert declared == expected, (
        f"{operation}.{member}: metric-service.smithy declares @{trait} {declared}, "
        f"coa_metrics.api.contract enforces {expected}. Update the constant to match the model."
    )


@pytest.mark.parametrize("operation", ["BulkDeleteMetrics", "ExportOsi"])
def test_name_list_members_are_metric_names(operation: str) -> None:
    text = _model()
    target, _ = _input_members(text, operation)["names"]
    member = re.search(rf"list\s+{target}\s*\{{\s*member:\s*(\w+)", text)
    assert member and member.group(1) == "MetricName"
    assert _bound(_shape_traits(text, "MetricName"), "length") == (
        contract.METRIC_NAME_MIN_LENGTH,
        contract.METRIC_NAME_MAX_LENGTH,
    )


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        ("dataSourceId", contract.BULK_DELETE_FILTER_DATA_SOURCE_ID_MAX_LENGTH),
        ("sourceTable", contract.BULK_DELETE_FILTER_SOURCE_TABLE_MAX_LENGTH),
        ("namePrefix", contract.BULK_DELETE_FILTER_NAME_PREFIX_MAX_LENGTH),
    ],
)
def test_bulk_delete_filter_bounds_match_model(member: str, expected: int) -> None:
    text = _model()
    declared = _resolved_bound(text, _structure_members(text, "BulkDeleteFilter")[member], "length")
    assert declared == (None, expected)


@pytest.mark.parametrize("operation", ["CreateMetric", "UpdateMetric"])
@pytest.mark.parametrize(
    ("member", "expected"),
    [
        ("description", contract.METRIC_DESCRIPTION_MAX_LENGTH),
        ("unit", contract.METRIC_UNIT_MAX_LENGTH),
        ("returnType", contract.METRIC_RETURN_TYPE_MAX_LENGTH),
        ("ontologyConcepts", contract.ONTOLOGY_CONCEPTS_MAX_ITEMS),
    ],
)
def test_metric_body_bounds_match_model(operation: str, member: str, expected: int) -> None:
    """The bounds OSI import applies per metric are the CreateMetric/UpdateMetric body bounds."""
    text = _model()
    assert _resolved_bound(text, _input_members(text, operation)[member], "length") == (None, expected)


@pytest.mark.parametrize(
    ("member", "expected_items", "list_shape", "expected_member_length"),
    [
        ("synonyms", contract.AI_CONTEXT_SYNONYMS_MAX_ITEMS, "SynonymList", contract.AI_CONTEXT_SYNONYM_MAX_LENGTH),
        ("examples", contract.AI_CONTEXT_EXAMPLES_MAX_ITEMS, "ExampleList", contract.AI_CONTEXT_EXAMPLE_MAX_LENGTH),
    ],
)
def test_ai_context_list_bounds_match_model(
    member: str, expected_items: int, list_shape: str, expected_member_length: int
) -> None:
    text = _model()
    ai = _structure_members(text, "MetricAiContext")
    assert ai[member][0] == list_shape
    assert _bound(ai[member][1], "length") == (None, expected_items)
    member_traits = re.search(rf"list\s+{list_shape}\s*\{{\s*(@length\([^)]*\))\s*member:", text)
    assert member_traits and _bound([member_traits.group(1)], "length") == (None, expected_member_length)


def test_ai_context_instructions_and_ontology_concept_bounds_match_model() -> None:
    text = _model()
    instructions = _structure_members(text, "MetricAiContext")["instructions"]
    assert _bound(instructions[1], "length") == (None, contract.AI_CONTEXT_INSTRUCTIONS_MAX_LENGTH)
    concept = re.search(r"list\s+OntologyConceptList\s*\{\s*(@length\([^)]*\))\s*member:", text)
    assert concept and _bound([concept.group(1)], "length") == (None, contract.ONTOLOGY_CONCEPT_MAX_LENGTH)


def test_data_source_id_pattern_matches_model() -> None:
    assert _pattern(_model(), "DataSourceId") == f"^{contract.DATA_SOURCE_ID_PATTERN.pattern}$"


def test_metric_label_bounds_and_pattern_match_model() -> None:
    text = _model()
    assert _bound(_shape_traits(text, "MetricLabel"), "length") == (
        contract.METRIC_LABEL_MIN_LENGTH,
        contract.METRIC_LABEL_MAX_LENGTH,
    )
    assert _pattern(text, "MetricLabel") == f"^{contract.METRIC_LABEL_PATTERN.pattern}$"
    # Same ASCII rule as NamespaceDisplayName (namespace.smithy), by contract #1295.
    namespace_model = (_SMITHY_DIR / "namespace.smithy").read_text()
    assert _pattern(text, "MetricLabel") == _pattern(namespace_model, "NamespaceDisplayName")


@pytest.mark.parametrize("operation", ["CreateMetric", "UpdateMetric", "ValidateMetric"])
def test_metric_label_input_members_target_metric_label(operation: str) -> None:
    target, traits = _input_members(_model(), operation)["label"]
    assert target == "MetricLabel"
    assert not any(t.startswith("@required") for t in traits)


def test_metric_definition_label_targets_metric_label() -> None:
    target, traits = _structure_members(_model(), "MetricDefinition")["label"]
    assert target == "MetricLabel"
    assert not any(t.startswith("@required") for t in traits)


@pytest.mark.parametrize(
    "value",
    ["", "x" * (contract.METRIC_LABEL_MAX_LENGTH + 1), "a<b", "a>b", "a\x00", "a\x1f", "a\x7f"],
    ids=["empty", "too-long", "lt", "gt", "nul", "us", "del"],
)
def test_check_metric_label_rejects_everything_the_model_pattern_and_length_reject(value: str) -> None:
    """The handwritten rule is never looser than the model, so OSI import cannot store what the API refuses."""
    assert contract.check_metric_label(value) is not None


# ── Enforcement ──────────────────────────────────────────────────────────────


def _event(method: str, resource: str, **kwargs: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "httpMethod": method,
        "resource": resource,
        "pathParameters": {"namespaceId": _NS, **kwargs.pop("path", {})},
        "queryStringParameters": kwargs.pop("query", None),
        "multiValueQueryStringParameters": kwargs.pop("multi", None),
        "requestContext": {"authorizer": {"email": "test@example.com"}},
    }
    if "body" in kwargs:
        event["body"] = json.dumps(kwargs.pop("body"))
    return event


def _assert_rejected(resp: dict[str, Any], field: str) -> None:
    assert resp["statusCode"] == 400, resp["body"]
    message = json.loads(resp["body"])["message"]
    assert field in message
    assert _POISON not in resp["body"]


@pytest.fixture
def neptune() -> Any:
    """A Neptune double shared by every metric handler module; asserting it untouched proves no backend call."""
    fake = MagicMock()
    modules = ("get_metric", "update_metric", "delete_metric", "list_metrics", "export_osi", "bulk_delete_metrics")
    patches = [patch(f"coa_metrics.api.{m}._get_neptune", return_value=fake) for m in modules]
    for p in patches:
        p.start()
    yield fake
    for p in patches:
        p.stop()


@pytest.mark.parametrize(
    ("method", "resource"),
    [
        ("GET", "/namespaces/{namespaceId}/metrics/{name}"),
        ("PUT", "/namespaces/{namespaceId}/metrics/{name}"),
        ("DELETE", "/namespaces/{namespaceId}/metrics/{name}"),
    ],
)
def test_over_long_metric_name_label_rejected(neptune: Any, method: str, resource: str) -> None:
    name = _POISON + "x" * contract.METRIC_NAME_MAX_LENGTH
    resp = metric_api_handler(_event(method, resource, path={"name": name}, body={}), None)
    _assert_rejected(resp, "name")
    assert not neptune.method_calls


def test_missing_metric_404_does_not_echo_name(neptune: Any) -> None:
    neptune.get_metric.return_value = None
    resp = metric_api_handler(_event("GET", "/namespaces/{namespaceId}/metrics/{name}", path={"name": "a<b>"}), None)
    assert resp["statusCode"] == 404
    assert "a<b>" not in resp["body"]


@pytest.mark.parametrize(
    ("query", "field"),
    [
        ({"maxResults": "abc"}, "maxResults"),
        ({"maxResults": "0"}, "maxResults"),
        ({"nextToken": _POISON}, "nextToken"),
        ({"nextToken": "1" * (contract.PAGINATION_TOKEN_MAX_LENGTH + 1)}, "nextToken"),
        ({"dataSourceId": _POISON}, "dataSourceId"),
        ({"dataSourceId": "d" * (contract.DATA_SOURCE_ID_MAX_LENGTH + 1)}, "dataSourceId"),
        ({"sourceTable": "t" * (contract.SOURCE_TABLE_MAX_LENGTH + 1)}, "sourceTable"),
    ],
)
def test_list_metrics_out_of_contract_query_rejected(neptune: Any, query: dict, field: str) -> None:
    resp = metric_api_handler(_event("GET", "/namespaces/{namespaceId}/metrics", query=query), None)
    _assert_rejected(resp, field)
    neptune.list_metrics.assert_not_called()


def test_list_metrics_page_size_above_cap_is_clamped(neptune: Any) -> None:
    neptune.list_metrics.return_value = []
    query = {"maxResults": "1000", "nextToken": "200"}
    resp = metric_api_handler(_event("GET", "/namespaces/{namespaceId}/metrics", query=query), None)
    assert resp["statusCode"] == 200
    kwargs = neptune.list_metrics.call_args.kwargs
    assert (kwargs["limit"], kwargs["offset"]) == (contract.LIST_METRICS_MAX_RESULTS_MAX + 1, 200)


def test_get_import_job_over_long_job_id_rejected() -> None:
    job_id = _POISON + "j" * contract.JOB_ID_MAX_LENGTH
    with patch("coa_metrics.api.get_import_job.get_job") as get_job:
        resp = metric_api_handler(
            _event("GET", "/namespaces/{namespaceId}/import-jobs/{jobId}", path={"jobId": job_id}), None
        )
    _assert_rejected(resp, "jobId")
    get_job.assert_not_called()


def test_get_import_job_404_does_not_echo_job_id() -> None:
    with patch("coa_metrics.api.get_import_job.get_job", return_value=None):
        resp = metric_api_handler(
            _event("GET", "/namespaces/{namespaceId}/import-jobs/{jobId}", path={"jobId": "a<b>"}), None
        )
    assert resp["statusCode"] == 404
    assert "a<b>" not in resp["body"]


def test_upload_url_over_long_filename_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OSI_BUCKET_NAME", "bucket")
    filename = "f" * contract.FILENAME_MAX_LENGTH + ".yaml"
    with patch("coa_metrics.api.get_osi_upload_url._get_s3_client") as s3:
        resp = metric_api_handler(
            _event("POST", "/namespaces/{namespaceId}/import-osi/upload-url", body={"filename": filename}), None
        )
    _assert_rejected(resp, "filename")
    s3.assert_not_called()


def test_upload_url_bad_extension_not_echoed() -> None:
    resp = metric_api_handler(
        _event("POST", "/namespaces/{namespaceId}/import-osi/upload-url", body={"filename": "x.<script>"}), None
    )
    assert resp["statusCode"] == 400
    assert "<script>" not in resp["body"]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"s3Key": f"{_NS}/imports/" + "k" * contract.IMPORT_S3_KEY_MAX_LENGTH}, "s3Key"),
        ({"content": {"not": "a string"}}, "content"),
    ],
)
def test_import_osi_out_of_contract_body_rejected(body: dict, field: str) -> None:
    with patch("coa_metrics.api.import_osi._read_from_s3") as read:
        resp = metric_api_handler(_event("POST", "/namespaces/{namespaceId}/import-osi", body=body), None)
    _assert_rejected(resp, field)
    read.assert_not_called()


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"names": ["m"] * (contract.BULK_DELETE_NAMES_MAX_ITEMS + 1)}, "names"),
        ({"names": "not-a-list"}, "names"),
        ({"names": [_POISON + "x" * contract.METRIC_NAME_MAX_LENGTH]}, "names"),
        ({"names": [42]}, "names"),
        ({"filter": "x"}, "filter"),
        ({"filter": {"dataSourceId": "d" * (contract.BULK_DELETE_FILTER_DATA_SOURCE_ID_MAX_LENGTH + 1)}}, "filter"),
        ({"filter": {"sourceTable": "t" * (contract.BULK_DELETE_FILTER_SOURCE_TABLE_MAX_LENGTH + 1)}}, "filter"),
        ({"filter": {"namePrefix": "p" * (contract.BULK_DELETE_FILTER_NAME_PREFIX_MAX_LENGTH + 1)}}, "filter"),
        ({"deleteAll": "yes"}, "deleteAll"),
    ],
)
def test_bulk_delete_out_of_contract_body_rejected(neptune: Any, body: dict, field: str) -> None:
    with patch("coa_metrics.api.bulk_delete_metrics._get_opensearch"):
        resp = metric_api_handler(_event("POST", "/namespaces/{namespaceId}/bulk-delete-metrics", body=body), None)
    _assert_rejected(resp, field)
    assert not neptune.method_calls


def test_bulk_delete_names_at_limit_accepted(neptune: Any) -> None:
    neptune.delete_metric.return_value = True
    body = {"names": ["m"] * contract.BULK_DELETE_NAMES_MAX_ITEMS}
    with patch("coa_metrics.api.bulk_delete_metrics._get_opensearch"):
        resp = metric_api_handler(_event("POST", "/namespaces/{namespaceId}/bulk-delete-metrics", body=body), None)
    assert resp["statusCode"] == 200


def test_export_osi_too_many_names_rejected(neptune: Any) -> None:
    names = ["m"] * (contract.EXPORT_NAMES_MAX_ITEMS + 1)
    resp = metric_api_handler(_event("GET", "/namespaces/{namespaceId}/export-osi", multi={"names": names}), None)
    _assert_rejected(resp, "names")
    neptune.get_metrics_bulk.assert_not_called()
