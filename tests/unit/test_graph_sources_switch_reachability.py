# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""With the graph-sources switch off, no entry point reaches graph-source behaviour.

One walk over the known ways in (the Sources API, graph verify, ontology import, the
REST data layer and serve), listed below. Each layer's own tests cover its gate in
detail; this one fails if any of these gates is lost. It can't find a new way in that
isn't listed. The last tests turn the switch on and show the same entry points open.
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_common.constants import StructuredBackend
from coa_common.sparql_config import GRAPH_SOURCES_DISABLED_MESSAGE, GRAPH_SOURCES_ENV, SparqlStore
from coa_control_plane_server.models.sparql_configuration import SparqlConfiguration
from coa_data_layer.contract import ContractViolation
from coa_ontology.catalog import ingest
from coa_ontology.catalog.graph_source_resolver import GraphSourceResolution
from coa_ontology.catalog.ingest import IngestValidationError
from coa_serve import main as serve_main
from coa_serve.clients.sources_registry import SourceComposition
from coa_serve.orchestrator import Orchestrator
from coa_sources.api import sources_handler
from coa_sources.database.graph_verify import handler as graph_verify
from rdflib import Graph, URIRef

# sources_handler first: database_routes imports it back, so importing database_routes
# on its own is circular.
database_routes = importlib.import_module("coa_sources.api.database_routes")
# The REST data layer reads its settings at import.
with patch.dict(
    "os.environ",
    {
        "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
        "ONTOLOGY_PROXY_LAMBDA_ARN": "arn:aws:lambda:us-east-1:123456789012:function:test-ontology-proxy",
        "AWS_REGION": "us-east-1",
        "ALLOWED_ORIGIN": "https://test.example.com",
    },
):
    data_layer = importlib.import_module("coa_data_layer.handler")

pytestmark = pytest.mark.unit

_NS = "ns-1"
_SRC = "src-1"
_HOST = "my-graph.cluster-abc.us-east-1.neptune.amazonaws.com"
_DR = "coa_sources.api.database_routes"
_SH = "coa_sources.api.sources_handler"
_GV = "coa_sources.database.graph_verify.handler"
_ONTOLOGY = """
@prefix owl:        <http://www.w3.org/2002/07/owl#> .
@prefix rdfs:       <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex:         <http://example.org/o#> .
@prefix coa_import: <http://coa.amazon.com/vocab/coa_import#> .
<http://example.org/o#> a owl:Ontology .
ex:Person a owl:Class ; rdfs:label "Person" ; coa_import:dataSourceName "my-neptune" .
"""


@pytest.fixture(autouse=True)
def _api_env(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://test.example.com")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv(GRAPH_SOURCES_ENV, raising=False)


def _body(response):
    return response["statusCode"], json.loads(response["body"]) if response.get("body") else {}


def _sparql_request():
    req = MagicMock()
    req.name = "support-graph"
    req.glue_configuration = None
    req.jdbc_configuration = None
    req.custom_connector_configuration = None
    req.databricks_sql_warehouse_configuration = None
    req.sparql_configuration = SparqlConfiguration.from_dict({"dataEndpoint": _HOST})
    req.metadata_enrichment_enabled = None
    return req


def _graph_row(status="APPROVED"):
    return {
        "PK": f"NS#{_NS}",
        "SK": f"SRC#{_SRC}",
        "sourceId": _SRC,
        "namespaceId": _NS,
        "name": "support-graph",
        "sourceType": "DATABASE",
        "sourceSubType": "RDF_SPARQL",
        "status": status,
        "queryable": status == "APPROVED",
        "configuration": json.dumps({"store": "neptune", "dataEndpoint": _HOST, "region": "us-east-1"}),
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
    }


def _create():
    dao, sqs = MagicMock(), MagicMock()
    with (
        patch(f"{_DR}._get_dao", return_value=dao),
        patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
        patch(f"{_DR}._get_sqs", return_value=sqs),
        patch(f"{_DR}._GRAPH_VERIFY_QUEUE_URL", "https://sqs/verify"),
        patch(f"{_DR}.adjust_namespace_source_count"),
        # The deployment region is read at import; pin it so the graph host's region always matches.
        patch(f"{_DR}._AWS_REGION", "us-east-1"),
    ):
        response = database_routes._create_database_source(_sparql_request(), _NS, namespace_language="en")
    return response, dao, sqs


def _rescan_daos():
    dao, scan_dao, sqs, ns_dao = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    dao.get.return_value = _graph_row()
    ns_dao.get.return_value = {"PK": f"NS#{_NS}", "SK": "METADATA"}
    patches = (
        patch(f"{_SH}._get_dao", return_value=dao),
        patch(f"{_SH}._get_scan_dao", return_value=scan_dao),
        patch(f"{_SH}._get_sqs", return_value=sqs),
        patch(f"{_SH}._get_ns_dao", return_value=ns_dao),
        patch(f"{_SH}._GRAPH_VERIFY_QUEUE_URL", "https://sqs/verify"),
    )
    return patches, dao, scan_dao, sqs


def _import_graph_bound_ontology():
    resolver = MagicMock(return_value={"my-neptune": GraphSourceResolution(source_id=_SRC)})
    graph = Graph().parse(data=_ONTOLOGY, format="turtle")
    classes = {URIRef("http://example.org/o#Person")}
    with patch.object(ingest, "resolve_graph_sources", resolver):
        try:
            return ingest._class_graphsource_map_from_payload(graph, classes, _NS, {}), resolver
        except IngestValidationError as exc:
            return exc, resolver


def _register_strategy():
    return serve_main._register_sparql_strategy(
        [],
        graph_client=AsyncMock(),
        llm_client=AsyncMock(),
        guardrail_id="",
        guardrail_id_provider=lambda: "",
        sources_registry=MagicMock(),
        vector_client=MagicMock(),
        oss_ontology_index="ont",
        firewall=MagicMock(),
    )


class TestEveryEntryPointIsClosedWhileOff:
    def test_registering_a_graph_source(self):
        response, dao, sqs = _create()
        assert _body(response) == (400, {"error": GRAPH_SOURCES_DISABLED_MESSAGE})
        dao.put.assert_not_called()
        sqs.send_message.assert_not_called()

    def test_re_verifying_a_graph_source(self):
        patches, dao, scan_dao, sqs = _rescan_daos()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = sources_handler._handle_rescan({}, _NS, _SRC)
        assert _body(response) == (400, {"error": GRAPH_SOURCES_DISABLED_MESSAGE})
        dao.update.assert_not_called()
        scan_dao.put.assert_not_called()
        sqs.send_message.assert_not_called()

    def test_a_scheduled_re_scan_of_a_graph_source(self):
        patches, dao, scan_dao, sqs = _rescan_daos()
        dao.get.return_value = {**_graph_row(), "rescanSchedule": {"enabled": True, "intervalHours": 24}}
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = sources_handler._handle_scheduled_rescan({"namespaceId": _NS, "sourceId": _SRC})
        assert response == {"ok": False, "statusCode": 400}
        dao.update.assert_not_called()
        sqs.send_message.assert_not_called()

    def test_a_verify_message_still_on_the_queue(self):
        dao, scan_dao, probe = MagicMock(), MagicMock(), MagicMock()
        dao.get.return_value = _graph_row("REGISTERED")
        event = {"Records": [{"messageId": "m-1", "body": json.dumps({"sourceId": _SRC, "namespaceId": _NS})}]}
        with (
            patch(f"{_GV}._get_dao", return_value=dao),
            patch(f"{_GV}._get_scan_dao", return_value=scan_dao),
            patch.dict(graph_verify.PROBES, {SparqlStore.NEPTUNE: probe}),
        ):
            graph_verify.handler(event, None)
        probe.assert_not_called()
        dao.update.assert_not_called()
        dao.put.assert_not_called()
        dao.transact_write.assert_not_called()
        scan_dao.update.assert_not_called()

    def test_importing_an_ontology_that_binds_a_class_to_a_graph_source(self):
        result, resolver = _import_graph_bound_ontology()
        assert isinstance(result, IngestValidationError)
        assert [f.code for f in result.findings] == ["graph_sources_disabled"]
        resolver.assert_not_called()

    def test_the_sparql_strategy_is_never_registered(self):
        assert _register_strategy() is None

    def test_a_rest_query_naming_nl_to_sparql_is_rejected_as_on_main(self):
        with pytest.raises(ContractViolation):
            data_layer._parse_query_options({"strategy": "nl_to_sparql"})

    def test_pinning_nl_to_sparql_runs_the_default_chain(self):
        assert Orchestrator._has_explicit_strategy({"strategy": "nl_to_sparql"}) is False
        assert Orchestrator._resolve_strategy_selection({"strategy": "nl_to_sparql"}, None) == (
            Orchestrator._resolve_strategy_selection({}, None)
        )

    @pytest.mark.asyncio
    async def test_a_serve_request_for_a_graph_only_namespace_skips_tier_2(self):
        registry = MagicMock()
        registry.get_source_composition = AsyncMock(
            return_value=SourceComposition(
                has_structured_source=True,
                has_unstructured_source=True,
                structured_backends=frozenset({StructuredBackend.RDF_SPARQL}),
            )
        )
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator._sources_registry = registry
        gating = await orchestrator._resolve_source_gating(_NS, None, MagicMock())
        assert gating.skip_tier2 is True
        assert StructuredBackend.RDF_SPARQL not in (gating.structured_backends or frozenset())


class TestTheSameEntryPointsOpenWhenOn:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setenv(GRAPH_SOURCES_ENV, "on")

    def test_registering_a_graph_source_is_accepted(self):
        response, dao, sqs = _create()
        assert response["statusCode"] == 202
        sqs.send_message.assert_called_once()

    def test_importing_a_graph_bound_ontology_binds_the_class(self):
        result, resolver = _import_graph_bound_ontology()
        assert result == {"http://example.org/o#Person": _SRC}
        resolver.assert_called_once()

    def test_the_sparql_strategy_is_registered_and_pinnable(self):
        assert _register_strategy() is not None
        assert Orchestrator._has_explicit_strategy({"strategy": "nl_to_sparql"}) is True
        assert data_layer._parse_query_options({"strategy": "nl_to_sparql"}) == {"strategy": "nl_to_sparql"}
