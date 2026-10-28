# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Query bounds, enum filters and the missing-ontology download, per the Smithy contract.

Each route here used to accept an out-of-contract value and answer 200:

* GetOntologyOverview ``limit`` / ``offset`` were unchecked, and ``limit=-1``
  silently dropped the last class (``items[0:-1]``).
* GraphSearch ``q`` had no 4000-character cap.
* An invalid enum filter (proposals ``status`` / ``source_type``, induction jobs
  ``status``, ontologies ``ontology_type``, graph search ``kind``) returned an
  empty list, or was ignored, instead of a 400.
* Downloading an ontology that does not exist wrote a prefix-only Turtle file to
  the artifacts bucket and returned a download URL instead of a 404.
* StartInduction accepted an empty ``datasource_ids`` for a table-based strategy
  and failed later in the worker.

Every AWS / graph call is mocked.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from coa_ontology import dynamo_store, induce_catalog, proposals
from coa_ontology.catalog.routers import graph, ontologies
from coa_ontology.catalog.turtle_export import turtle_has_triples
from coa_ontology.main import app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

client = TestClient(app)

_SMITHY_DIR = Path(__file__).resolve().parents[4] / "models" / "src" / "main" / "smithy"
_GRAPH_MODEL = (_SMITHY_DIR / "ontology-graph.smithy").read_text()
_INDUCTION_MODEL = (_SMITHY_DIR / "ontology-induction.smithy").read_text()

_BOGUS = "BOGUS-filter-value"


def _member_traits(text: str, operation: str, member: str) -> str:
    """The trait lines just above ``member`` in ``operation``'s inline input."""
    op = text.index(f"operation {operation} {{")
    body = text[op : text.index("output", op)]
    m = re.search(rf"((?:\s*(?:@|///)[^\n]*\n)+)\s*{member}\s*:", body)
    assert m, f"{operation}.{member} not found"
    return m.group(1)


def _enum_values(text: str, shape: str) -> list[str]:
    m = re.search(rf"enum\s+{shape}\s*\{{(.*?)\n\}}", text, re.S)
    assert m, f"enum {shape} not found"
    return re.findall(r'=\s*"([^"]+)"', m.group(1))


# ── Parity with the model ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("member", "trait", "expected"),
    [
        ("limit", r"@range\(min:\s*(\d+),\s*max:\s*(\d+)\)", (1, graph._MAX_OVERVIEW_LIMIT)),
        ("offset", r"@range\(min:\s*(\d+),\s*max:\s*(\d+)\)", (0, graph._MAX_OVERVIEW_OFFSET)),
    ],
)
def test_overview_bounds_equal_the_model(member: str, trait: str, expected: tuple[int, int]) -> None:
    m = re.search(trait, _member_traits(_GRAPH_MODEL, "GetOntologyOverview", member))
    assert m
    assert (int(m.group(1)), int(m.group(2))) == expected


def test_search_q_length_equals_the_model() -> None:
    """The model sets only a maximum, like the server: an empty ``q`` lists everything."""
    m = re.search(r"@length\(max:\s*(\d+)\)", _member_traits(_GRAPH_MODEL, "GraphSearch", "q"))
    assert m
    assert int(m.group(1)) == graph._MAX_SEARCH_QUERY_LENGTH


# ── GetOntologyOverview limit / offset ───────────────────────────────────────


def _overview(n_classes: int) -> dict[str, Any]:
    return {
        "ontology_id": "https://example.org/onto#",
        "graph_uri": "g",
        "classes": [{"uri": f"https://example.org/onto#C{i}", "label": f"C{i}"} for i in range(n_classes)],
        "object_properties": [],
        "datatype_properties": [],
    }


@pytest.fixture()
def graph_store() -> Any:
    store = MagicMock()
    store.get_ontology_overview.return_value = _overview(13)
    store.search_entities.return_value = []
    store.count_entities.return_value = 0
    with (
        patch("coa_ontology.catalog.routers.graph.build_stores", return_value=(store, MagicMock())),
        patch("coa_ontology.catalog.routers.graph.label_preference_for", return_value=None),
    ):
        yield store


@pytest.mark.parametrize(
    "params",
    [
        {"limit": -1},
        {"limit": 0},
        {"limit": graph._MAX_OVERVIEW_LIMIT + 1},
        {"offset": -1},
        {"offset": graph._MAX_OVERVIEW_OFFSET + 1},
    ],
    ids=["limit-negative", "limit-zero", "limit-over-max", "offset-negative", "offset-over-max"],
)
def test_overview_out_of_range_is_400_before_any_read(graph_store: Any, params: dict[str, int]) -> None:
    resp = client.get("/graph/ontology-overview", params={"ontology_id": "o", "namespace": "ns", **params})
    assert resp.status_code == 400
    assert "must be between" in resp.json()["detail"]
    graph_store.get_ontology_overview.assert_not_called()


@pytest.mark.parametrize(
    ("params", "n_classes"),
    [
        ({}, 13),
        ({"limit": 1}, 1),
        ({"limit": graph._MAX_OVERVIEW_LIMIT}, 13),
        ({"limit": 100, "offset": 12}, 1),
        ({"offset": graph._MAX_OVERVIEW_OFFSET}, 0),
    ],
    ids=["no-paging", "limit-min", "limit-max", "last-item", "offset-max"],
)
def test_overview_in_range_pages_as_before(graph_store: Any, params: dict[str, int], n_classes: int) -> None:
    resp = client.get("/graph/ontology-overview", params={"ontology_id": "o", "namespace": "ns", **params})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["classes"]) == n_classes
    assert body["totalClasses"] == 13


# ── GraphSearch q / kind ─────────────────────────────────────────────────────


def test_search_q_over_max_length_is_400(graph_store: Any) -> None:
    q = "x" * (graph._MAX_SEARCH_QUERY_LENGTH + 1)
    resp = client.get("/graph/search", params={"q": q, "namespace": "ns"})
    assert resp.status_code == 400
    assert q not in resp.text
    graph_store.search_entities.assert_not_called()


def test_search_q_at_max_length_is_accepted(graph_store: Any) -> None:
    resp = client.get("/graph/search", params={"q": "x" * graph._MAX_SEARCH_QUERY_LENGTH, "namespace": "ns"})
    assert resp.status_code == 200


def test_search_empty_q_still_lists_everything(graph_store: Any) -> None:
    """The UI sends ``q=`` to load a namespace's entities, so no minimum is enforced."""
    assert client.get("/graph/search", params={"q": "", "namespace": "ns"}).status_code == 200


def test_search_invalid_kind_is_400(graph_store: Any) -> None:
    resp = client.get("/graph/search", params={"q": "x", "namespace": "ns", "kind": _BOGUS})
    assert resp.status_code == 400
    assert _BOGUS not in resp.text
    assert "kind must be one of" in resp.json()["detail"]
    graph_store.search_entities.assert_not_called()


@pytest.mark.parametrize("kind", _enum_values(_GRAPH_MODEL, "GraphVertexKind"))
def test_search_every_model_kind_is_accepted(graph_store: Any, kind: str) -> None:
    resp = client.get("/graph/search", params={"q": "x", "namespace": "ns", "kind": kind})
    assert resp.status_code == 200
    assert graph_store.search_entities.call_args.kwargs["kind"] == kind


# ── Enum filters on the list routes ──────────────────────────────────────────


@pytest.fixture()
def proposals_page() -> Any:
    with patch.object(proposals.dynamo_store, "list_proposals_newest_page", return_value=([], None)) as page:
        yield page


@pytest.mark.parametrize("param", ["status", "source_type"])
def test_list_proposals_invalid_enum_is_400(proposals_page: Any, param: str) -> None:
    resp = client.get("/ontology/proposals/", params={"namespace": "ns", param: _BOGUS})
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith(f"{param} must be one of: ")
    assert _BOGUS not in resp.text
    proposals_page.assert_not_called()


@pytest.mark.parametrize(
    ("param", "value"),
    [("status", v) for v in _enum_values(_INDUCTION_MODEL, "ProposalStatus")]
    + [("source_type", v) for v in _enum_values(_INDUCTION_MODEL, "InductionSourceType")],
)
def test_list_proposals_every_model_value_is_forwarded(proposals_page: Any, param: str, value: str) -> None:
    resp = client.get("/ontology/proposals/", params={"namespace": "ns", param: value})
    assert resp.status_code == 200
    assert proposals_page.call_args.kwargs[param] == value


def test_list_proposals_without_filters_is_unchanged(proposals_page: Any) -> None:
    resp = client.get("/ontology/proposals/", params={"namespace": "ns"})
    assert resp.status_code == 200
    assert resp.json() == {"proposals": []}
    assert proposals_page.call_args.kwargs["status"] is None


@pytest.fixture()
def jobs_page() -> Any:
    with patch.object(induce_catalog.dynamo_store, "list_jobs_page", return_value=([], None)) as page:
        yield page


def test_list_jobs_invalid_status_is_400(jobs_page: Any) -> None:
    resp = client.get("/induce/jobs", params={"namespace": "ns", "status": _BOGUS})
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("status must be one of: ")
    jobs_page.assert_not_called()


@pytest.mark.parametrize("status", [*_enum_values(_INDUCTION_MODEL, "InductionJobStatus"), "ingested", "cancelled"])
def test_list_jobs_every_model_status_is_forwarded(jobs_page: Any, status: str) -> None:
    """Every model status, plus the stored ``ingested`` / ``cancelled`` the model does not list."""
    resp = client.get("/induce/jobs", params={"namespace": "ns", "status": status})
    assert resp.status_code == 200
    assert jobs_page.call_args.kwargs["status"] == status


@pytest.fixture()
def ontologies_page() -> Any:
    with patch.object(ontologies.dynamo_store, "list_ontologies_registry_page", return_value=([], None)) as page:
        yield page


def test_list_ontologies_invalid_type_is_400(ontologies_page: Any) -> None:
    resp = client.get("/ontologies/", params={"namespace": "ns", "ontology_type": _BOGUS})
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("ontology_type must be one of: ")
    ontologies_page.assert_not_called()


@pytest.mark.parametrize("ontology_type", _enum_values(_GRAPH_MODEL, "OntologyType"))
def test_list_ontologies_every_model_type_is_forwarded(ontologies_page: Any, ontology_type: str) -> None:
    resp = client.get("/ontologies/", params={"namespace": "ns", "ontology_type": ontology_type})
    assert resp.status_code == 200
    assert ontologies_page.call_args.kwargs["ontology_type"] == ontology_type


@pytest.mark.parametrize(
    ("path", "param", "page_fixture"),
    [
        ("/ontology/proposals/", "status", "proposals_page"),
        ("/induce/jobs", "status", "jobs_page"),
        ("/ontologies/", "ontology_type", "ontologies_page"),
        ("/graph/search", "kind", "graph_store"),
    ],
)
def test_empty_enum_filter_means_no_filter(request: Any, path: str, param: str, page_fixture: str) -> None:
    """A bare ``?status=`` (and the like) has always been treated as absent, so it is not a 400."""
    page = request.getfixturevalue(page_fixture)
    resp = client.get(path, params={"namespace": "ns", "q": "x", param: ""})
    assert resp.status_code == 200
    call = page.search_entities.call_args if page_fixture == "graph_store" else page.call_args
    assert not call.kwargs[param]


# ── ontology_type on the write routes: the store and the filter agree ────────

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_ONTOLOGY_TYPES = _enum_values(_GRAPH_MODEL, "OntologyType")
# An ``ontology_type`` literal the code writes or defaults to:
# ``"ontology_type": "x"``, ``ontology_type="x"``, ``ontology_type: str = "x"``,
# ``ontology_type: str = Form("x")`` and ``_INDUCED_ONTOLOGY_TYPE = "x"``.
_TYPE_LITERAL = re.compile(r"""ontology_type["']?\s*(?::\s*str\s*)?[:=]\s*(?:Form\()?["']([a-z_]+)["']""", re.I)


_YAML_TYPE = re.compile(r"^\s*ontology_type:\s*([a-z_]+)\s*$", re.M)


def _type_literals(path: Path) -> set[str]:
    pattern = _YAML_TYPE if path.suffix == ".yaml" else _TYPE_LITERAL
    return set(pattern.findall(path.read_text()))


def test_every_ontology_type_the_service_writes_is_filterable() -> None:
    """No row the service stores itself can be left unfilterable by the list route."""
    written: set[str] = set()
    for path in (_ENGINE_ROOT / "src").rglob("*.py"):
        written |= _type_literals(path)
    assert {"foundational", "induced", "user_created"} <= written  # the scan finds the writers
    assert written <= set(_ONTOLOGY_TYPES), sorted(written - set(_ONTOLOGY_TYPES))


@pytest.mark.parametrize("ontology_type", ["foundational", "induced", "user_created"])
def test_list_ontologies_accepts_every_self_written_type(ontologies_page: Any, ontology_type: str) -> None:
    resp = client.get("/ontologies/", params={"namespace": "ns", "ontology_type": ontology_type})
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "path", ["demo/demo.py", "demo/config-example.yaml", "demo/setup.py", "scripts/live_foundational_upload.py"]
)
def test_demo_and_scripts_send_only_model_types(path: str) -> None:
    sent = _type_literals(_ENGINE_ROOT / path)
    if path == "demo/config-example.yaml":
        assert sent  # the customer tier's type is declared here
    if path == "demo/demo.py":
        assert "customer_provided" not in (_ENGINE_ROOT / path).read_text()
    assert sent <= set(_ONTOLOGY_TYPES), sorted(sent - set(_ONTOLOGY_TYPES))


def test_ingest_from_s3_unknown_type_is_rejected_before_any_write() -> None:
    """The one write route the deployed API serves: its generated model already
    holds ``ontologyType`` to the enum (the engine's 422 body-validation answer)."""
    with patch.object(ontologies.dynamo_store, "put_ingest_job") as put_job:
        resp = client.post(
            "/ontologies/x/ingest-from-s3",
            params={"namespace": "ns"},
            json={"s3Key": "ontology-uploads/ns/u/o.ttl", "ontologyType": "customer_provided"},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "ontologyType"]
    put_job.assert_not_called()


@pytest.fixture()
def write_mocks() -> Any:
    graph_store = MagicMock()
    with (
        patch.object(ontologies, "_graph", return_value=graph_store) as get_graph,
        patch.object(ontologies.dynamo_store, "get_ontology_registry", return_value=None) as get_reg,
        patch.object(ontologies.dynamo_store, "put_ontology_registry") as put_reg,
        patch.object(ontologies, "_persist_upload") as persist,
    ):
        yield {"graph": get_graph, "store": graph_store, "get_reg": get_reg, "put_reg": put_reg, "persist": persist}


def _create(ontology_type: str) -> Any:
    body = {"uri": "http://x.invalid/onto", "title": "t", "ontology_type": ontology_type}
    return client.post("/ontologies/", params={"namespace": "ns"}, json=body)


def _upload(ontology_type: str) -> Any:
    return client.post(
        "/ontologies/http%3A%2F%2Fx.invalid%2Fonto/upload",
        data={"namespace": "ns", "ontology_type": ontology_type},
        files={"file": ("o.ttl", b"@prefix ex: <http://x.invalid/> .", "text/turtle")},
    )


@pytest.mark.parametrize("send", [_create, _upload], ids=["create", "upload"])
@pytest.mark.parametrize("ontology_type", [_BOGUS, "customer_provided"])
def test_write_with_unknown_type_is_400_and_writes_nothing(write_mocks: Any, send: Any, ontology_type: str) -> None:
    resp = send(ontology_type)
    assert resp.status_code == 400
    assert resp.json()["detail"] == f"ontology_type must be one of: {', '.join(_ONTOLOGY_TYPES)}"
    write_mocks["get_reg"].assert_not_called()
    write_mocks["put_reg"].assert_not_called()
    write_mocks["persist"].assert_not_called()
    write_mocks["store"].create_ontology.assert_not_called()


@pytest.mark.parametrize("send", [_create, _upload], ids=["create", "upload"])
@pytest.mark.parametrize("ontology_type", _ONTOLOGY_TYPES)
def test_write_with_model_type_passes_the_check(write_mocks: Any, send: Any, ontology_type: str) -> None:
    # An existing row turns the request into a 409 right after the type check,
    # so a 409 (not a 400) shows the value got past it.
    write_mocks["get_reg"].return_value = {"uri": "http://x.invalid/onto"}
    assert send(ontology_type).status_code == 409


# ── DownloadOntology for an ontology that does not exist ─────────────────────

_PREFIX_ONLY = (
    "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "\n# no triples\n"
)
_ONE_TRIPLE = _PREFIX_ONLY + "<https://example.org/onto#> a owl:Ontology .\n"


@pytest.fixture()
def download_mocks() -> Any:
    store = MagicMock()
    store.get_ontology.return_value = None
    with (
        patch.object(ontologies, "_graph", return_value=store),
        patch.object(dynamo_store, "get_ontology_registry", return_value=None),
        patch.object(dynamo_store, "write_and_presign_ontology_download", return_value="https://s3/x") as write,
    ):
        yield store, write


def test_download_missing_ontology_is_404_and_writes_nothing(download_mocks: Any) -> None:
    store, write = download_mocks
    store.get_graph_turtle.return_value = _PREFIX_ONLY
    resp = client.get("/ontologies/http%3A%2F%2Fx.invalid%2Fnope/download", params={"namespace": "ns"})
    assert resp.status_code == 404
    write.assert_not_called()


def test_download_ontology_with_triples_is_unchanged(download_mocks: Any) -> None:
    store, write = download_mocks
    store.get_graph_turtle.return_value = _ONE_TRIPLE
    resp = client.get("/ontologies/https%3A%2F%2Fexample.org%2Fonto%23/download", params={"namespace": "ns"})
    assert resp.status_code == 200
    assert resp.json() == {"downloadUrl": "https://s3/x", "expiresInSeconds": 3600}
    write.assert_called_once()


@pytest.mark.parametrize(
    ("turtle", "expected"),
    [
        ("", False),
        ("   \n\n", False),
        (_PREFIX_ONLY, False),
        ("PREFIX owl: <http://www.w3.org/2002/07/owl#>\nBASE <http://x/>\n", False),
        ("@base <http://x/> .\n", False),
        (_ONE_TRIPLE, True),
        ("prefix:Thing a owl:Class .\n", True),
        ("<http://x/a> <http://x/p> <http://x/b> .", True),
        ("@prefix ex: <http://ex/> . ex:A a ex:Class .", True),
        ("@prefix ex: <http://ex/#> .  # trailing comment\n", False),
    ],
    ids=[
        "empty",
        "blank",
        "prefix-only",
        "sparql-directives",
        "base",
        "one-triple",
        "prefixed-subject",
        "no-prefix",
        "directive-and-triple-on-one-line",
        "hash-iri-and-comment",
    ],
)
def test_turtle_has_triples(turtle: str, expected: bool) -> None:
    assert turtle_has_triples(turtle) is expected


# ── StartInduction with no datasources ───────────────────────────────────────


@pytest.mark.parametrize("strategy", ["table_to_ontology", "rigor_ontology"])
def test_start_induction_table_strategy_without_datasources_is_400(strategy: str) -> None:
    with (
        patch.object(induce_catalog, "namespace_language_or_404") as language,
        patch.object(induce_catalog, "dynamo_store") as store,
    ):
        resp = client.post(
            "/induce/",
            params={"namespace": "ns"},
            json={"datasource_ids": [], "ontology_uri_prefix": "http://example.org/o#", "strategy": strategy},
        )
    assert resp.status_code == 400
    assert "datasource_ids" in resp.json()["detail"]
    language.assert_not_called()
    assert not store.method_calls


def test_start_induction_default_strategy_without_datasources_is_400() -> None:
    with patch.object(induce_catalog, "namespace_language_or_404") as language:
        resp = client.post(
            "/induce/", params={"namespace": "ns"}, json={"ontology_uri_prefix": "http://example.org/o#"}
        )
    assert resp.status_code == 400
    language.assert_not_called()


def test_start_induction_unstructured_still_accepts_no_datasources() -> None:
    """The lexical-graph strategy records datasource_ids as metadata only, so [] stays valid."""
    with (
        patch.object(induce_catalog, "namespace_language_or_404", return_value="en"),
        patch.dict(client.app.state.config, {"neptune_endpoint": ""}),
    ):
        resp = client.post(
            "/induce/",
            params={"namespace": "ns"},
            json={
                "datasource_ids": [],
                "ontology_uri_prefix": "http://example.org/o#",
                "strategy": "unstructured_lexical_graph",
            },
        )
    # Past the datasource check: the request reaches the lexical-graph source check.
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "CONFIGURATION_ERROR"


def test_start_induction_with_a_datasource_passes_the_check() -> None:
    with (
        patch.object(induce_catalog, "namespace_language_or_404", side_effect=RuntimeError("reached")),
        pytest.raises(RuntimeError, match="reached"),
    ):
        client.post(
            "/induce/",
            params={"namespace": "ns"},
            json={"datasource_ids": ["ds-1"], "ontology_uri_prefix": "http://example.org/o#"},
        )
