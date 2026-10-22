# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The bounds the ontology engine enforces must equal the ones its Smithy models declare.

Two halves:

* **Parity** — parses ``ontology-induction.smithy`` / ``ontology-graph.smithy``
  (the source of truth, not the generated OpenAPI, so this proves the model and
  the runtime agree rather than that codegen ran) and asserts every runtime
  constant matches its ``@range`` max, enum, or documented bound. Edit the model
  and forget the constant, or the reverse, and this goes red.
* **Enforcement / compatibility** — out-of-contract values are rejected (never
  clamped) without echoing them, and the new ``@pattern`` constraints accept
  what callers send today and nothing the runtime would itself refuse.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from coa_ontology import induce_catalog, proposals
from coa_ontology.catalog import ingest
from coa_ontology.catalog.routers import graph, ontologies
from coa_ontology.main import app
from coa_ontology.stores.neptune_db_graph import _iri
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_SMITHY_DIR = Path(__file__).resolve().parents[4] / "models" / "src" / "main" / "smithy"
_OPENAPI = (
    Path(__file__).resolve().parents[4]
    / "smithy-generated"
    / "openapi"
    / "openapi"
    / "ControlPlaneService.openapi.json"
)

client = TestClient(app)


# ── Minimal Smithy reader ────────────────────────────────────────────────────


def _model(name: str) -> str:
    path = _SMITHY_DIR / name
    assert path.is_file(), f"{name} not found at {path}"
    return path.read_text()


def _input_member_traits(text: str, operation: str, member: str) -> list[str]:
    """Trait lines (and doc lines) attached to ``member`` of ``operation``'s inline input."""
    op = re.search(rf"operation\s+{operation}\s*\{{", text)
    assert op, f"operation {operation} not found"
    inp = re.search(r"input\s*:=\s*(?:with\s*\[[^\]]*\]\s*)?\{", text[op.end() :])
    assert inp, f"{operation} input block not found"
    traits: list[str] = []
    depth = 1
    for line in text[op.end() + inp.end() :].splitlines():
        stripped = line.strip()
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0:
            break
        if stripped.startswith(("@", "///")):
            traits.append(stripped)
        elif m := re.match(r"([A-Za-z_]\w*)\s*:", stripped):
            if m.group(1) == member:
                return traits
            traits = []
    raise AssertionError(f"{operation}.{member} not found")


def _range_max(text: str, operation: str, member: str) -> int:
    for trait in _input_member_traits(text, operation, member):
        if m := re.match(r"@range\(.*max:\s*(\d+)", trait):
            return int(m.group(1))
    raise AssertionError(f"{operation}.{member} has no @range max")


def _enum_values(text: str, shape: str) -> set[str]:
    m = re.search(rf"enum\s+{shape}\s*\{{(.*?)\n\}}", text, re.S)
    assert m, f"enum {shape} not found"
    return set(re.findall(r'=\s*"([^"]+)"', m.group(1)))


_INDUCTION = _model("ontology-induction.smithy")
_GRAPH = _model("ontology-graph.smithy")


# ── Parity ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "operation", "member", "constant"),
    [
        (_INDUCTION, "ListProposals", "maxResults", proposals.LIST_PROPOSALS_MAX_RESULTS),
        (_INDUCTION, "ListInductionJobs", "maxResults", induce_catalog.LIST_INDUCTION_JOBS_MAX_RESULTS),
        (_INDUCTION, "ListInducedDatasources", "maxResults", induce_catalog.LIST_INDUCED_DATASOURCES_MAX_RESULTS),
        (_GRAPH, "ListOntologies", "maxResults", ontologies.LIST_ONTOLOGIES_MAX_RESULTS),
        (_GRAPH, "ListOntologies", "limit", ontologies.LIST_ONTOLOGIES_MAX_RESULTS),
        (_GRAPH, "GraphSearch", "limit", graph._MAX_SEARCH_LIMIT),
        (_GRAPH, "GraphSearch", "offset", graph._MAX_SEARCH_OFFSET),
    ],
    ids=lambda v: v if isinstance(v, str) and len(v) < 40 else "",
)
def test_runtime_page_bound_equals_the_model(text, operation, member, constant):
    assert constant == _range_max(text, operation, member)


def _shape_length_max(text: str, shape: str) -> int:
    m = re.search(rf"@length\(min:\s*\d+,\s*max:\s*(\d+)\)\s*\nstring\s+{shape}\b", text)
    assert m, f"string {shape} with @length not found"
    return int(m.group(1))


def _length_max(text: str, operation: str, member: str) -> int:
    for trait in _input_member_traits(text, operation, member):
        if m := re.match(r"@length\(.*max:\s*(\d+)", trait):
            return int(m.group(1))
    raise AssertionError(f"{operation}.{member} has no @length max")


@pytest.mark.parametrize("member", ["groundingOverrides", "novelTables"])
def test_grounding_choice_count_bound_equals_the_model(member):
    assert _length_max(_INDUCTION, "UpdateProposal", member) == proposals.MAX_GROUNDING_CHOICES


def test_grounding_key_and_target_bounds_equal_the_model():
    assert _shape_length_max(_INDUCTION, "GroundingSourceTableKey") == proposals.MAX_GROUNDING_SOURCE_TABLE_KEY_LENGTH
    assert _shape_length_max(_INDUCTION, "GroundingOverrideTargetUri") == proposals.MAX_GROUNDING_TARGET_URI_LENGTH


@pytest.mark.parametrize(
    "body",
    [
        {"groundingOverrides": {f"t{i}": "https://example.com/C" for i in range(1001)}},
        {"novelTables": [f"t{i}" for i in range(1001)]},
        {"groundingOverrides": {"": "https://example.com/C"}},
        {"groundingOverrides": {"t": "https://example.com/" + "x" * 2048}},
        {"novelTables": ["x" * 1025]},
    ],
    ids=["too-many-overrides", "too-many-novel", "empty-key", "long-target", "long-novel-key"],
)
def test_out_of_bound_grounding_choices_are_rejected(body):
    with pytest.raises(ValueError):
        proposals.UpdateProposalRequest.model_validate({"proposalId": "p-1", **body})


def test_constraint_config_bound_equals_the_documented_one():
    doc = " ".join(_input_member_traits(_INDUCTION, "CompileConstraints", "constraint_config"))
    m = re.search(r"At most (\d+)\s+bytes", doc)
    assert m, "CompileConstraints.constraint_config no longer documents its size bound"
    assert int(m.group(1)) == proposals.MAX_CONSTRAINT_CONFIG_BYTES


def test_ingest_format_enum_is_exactly_what_the_parser_understands():
    """Every enum value must parse as a real format, and every parser spelling be accepted."""
    assert _enum_values(_GRAPH, "OntologyFileFormat") == set(ingest._RDFLIB_FORMATS)


def test_ingest_ontology_type_enum_covers_what_the_web_app_sends():
    # ``user_uploaded``: services/ontology-engine.ts uploadOntologyFile.
    assert {"foundational", "induced", "user_created", "user_uploaded"} <= _enum_values(_GRAPH, "OntologyType")


# ── Enforcement ──────────────────────────────────────────────────────────────


@pytest.fixture()
def _graph_store():
    store = MagicMock()
    store.search_entities.return_value = []
    store.count_entities.return_value = 0
    with patch("coa_ontology.catalog.routers.graph.build_stores", return_value=(store, MagicMock())):
        yield store


@pytest.mark.parametrize("offset", [-1, graph._MAX_SEARCH_OFFSET + 1])
def test_graph_search_offset_out_of_range_is_400(_graph_store, offset):
    resp = client.get("/graph/search", params={"q": "x", "namespace": "ns", "offset": offset})
    assert resp.status_code == 400
    _graph_store.search_entities.assert_not_called()


def test_graph_search_offset_at_the_bound_is_accepted(_graph_store):
    resp = client.get("/graph/search", params={"q": "x", "namespace": "ns", "offset": graph._MAX_SEARCH_OFFSET})
    assert resp.status_code == 200


def test_oversized_constraint_config_is_400_before_any_read():
    big = {"constraints": ["x" * 1024] * (proposals.MAX_CONSTRAINT_CONFIG_BYTES // 1024 + 1)}
    with patch.object(proposals.dynamo_store, "get_proposal_by_id") as get:
        resp = client.post("/ontology/proposals/p-1/compile-constraints?namespace=ns", json={"constraint_config": big})
    assert resp.status_code == 400
    assert "xxxx" not in resp.text
    get.assert_not_called()


def test_constraint_config_within_the_bound_reaches_the_proposal_lookup():
    with patch.object(proposals.dynamo_store, "get_proposal_by_id", return_value=None) as get:
        resp = client.post("/ontology/proposals/p-1/compile-constraints?namespace=ns", json={"constraint_config": {}})
    assert resp.status_code == 404
    get.assert_called_once()


# ── New @pattern constraints vs. today's callers and the runtime ─────────────


def _openapi_pattern(operation: str, member: str) -> re.Pattern[str]:
    """The pattern the generated OpenAPI carries for one input member (query param or body field)."""
    if not _OPENAPI.is_file():
        pytest.skip(f"{_OPENAPI} not found — run `make generate`")
    spec = json.loads(_OPENAPI.read_text())
    for item in spec["paths"].values():
        for op in item.values():
            if not isinstance(op, dict) or op.get("operationId") != operation:
                continue
            for param in op.get("parameters", []):
                if param["name"] == member:
                    return re.compile(param["schema"]["pattern"])
            ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
            prop = spec["components"]["schemas"][ref]["properties"][member]
            if "$ref" in prop:
                prop = spec["components"]["schemas"][prop["$ref"].rsplit("/", 1)[-1]]
            return re.compile(prop["pattern"])
    raise AssertionError(f"{operation}.{member} not in the OpenAPI spec")


_VERTEX_IRIS_IN_USE = [
    "http://example.com/onto#Customer",
    "https://schema.org/Order",
    "https://w3id.org/fibo/ontology/FND/Accounting/Account",
    "urn:ontology:upload-1a2b3c4d#Claim",
]


@pytest.mark.parametrize("operation", ["GetGraphClass", "GetGraphObjectProperty", "GetGraphDatatypeProperty"])
def test_vertex_iri_pattern_accepts_iris_in_use_and_only_what_the_runtime_accepts(operation):
    pattern = _openapi_pattern(operation, "uri")
    for iri in _VERTEX_IRIS_IN_USE:
        assert pattern.search(iri), iri
        _iri(iri)  # the runtime accepts it too
    for bad in ["ftp://x/y", "http:/no-host", "http://x/a b", "http://x/>inject", "relative#Cls", "_:b0"]:
        assert not pattern.search(bad), bad


def test_fetch_source_url_pattern_is_https_only():
    pattern = _openapi_pattern("FetchOntology", "sourceUrl")
    assert pattern.search("https://xmlns.com/foaf/spec/index.rdf")
    assert not pattern.search("http://xmlns.com/foaf/spec/index.rdf")


def test_graph_arn_pattern_accepts_the_benchmark_sentinel_and_graph_arns():
    pattern = _openapi_pattern("StartInduction", "graphArn")
    # ``neptune-db``: benchmarks/*/setup induce requests; the rest are answered 422 UNSUPPORTED_SOURCE.
    for value in ["neptune-db", "g-abc1234567", "arn:aws:neptune-graph:us-east-1:123456789012:graph/g-abc1234567"]:
        assert pattern.search(value), value
    assert not pattern.search("arn:aws:s3:::bucket")


@pytest.mark.parametrize("field", ["sourceUrl", "source_url"])
def test_fetch_request_reads_the_contract_and_legacy_source_url_spelling(field):
    from coa_ontology.catalog.schemas import OntologyFetchRequest

    assert (
        OntologyFetchRequest.model_validate({field: "https://example.com/o.ttl"}).source_url
        == "https://example.com/o.ttl"
    )
