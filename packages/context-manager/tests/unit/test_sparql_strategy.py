# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SparqlStrategy: translate → route to the class's source → firewall → execute."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_common.constants import StructuredBackend
from coa_serve.clients.base import VectorHit as BaseVectorHit
from coa_serve.clients.sparql_clients import SparqlClientCache
from coa_serve.deadline import Deadline
from coa_serve.exceptions import AccessDeniedError
from coa_serve.tier2.ontop.nl_to_sparql import TranslationResult
from coa_serve.tier2.ontop.tbox_context import TBoxContext
from coa_serve.tier2.sparql.strategy import SparqlStrategy, _bindings_to_rows
from coa_serve.tier2.sql_firewall import FirewallResult
from coa_serve.tier2.strategy import StrategyContext, StrategyOption
from coa_serve.trace import TraceCollector

pytestmark = pytest.mark.unit

_NS = "ns-graph"
_HOST = "db.cluster-abc.us-east-1.neptune.amazonaws.com"
_G = "https://example.test/ns/acme/data"
_SPARQL = "PREFIX ex: <http://example.org/o#>\nSELECT ?ticket ?title WHERE { ?ticket a ex:Ticket ; ex:title ?title }"


_PLATFORM = "coa-platform.cluster-abc.us-east-1.neptune.amazonaws.com"
_PLATFORM_HOSTS = (
    _PLATFORM,
    "coa-platform.cluster-ro-abc.us-east-1.neptune.amazonaws.com",
    "anything.cluster-custom-abc.us-east-1.neptune.amazonaws.com",
)


@pytest.fixture(autouse=True)
def _platform_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve names COA's own catalog cluster (NEPTUNE_ENDPOINT); the test source is another cluster."""
    monkeypatch.setenv("NEPTUNE_ENDPOINT", _PLATFORM)


_NS_ID = "ns-graph-id"
_CLAIM_PK = "GRAPHCLUSTER#us-east-1#cluster-RES1"


def _source(source_id="src-g1", *, sub_type="RDF_SPARQL", queryable=True, status=None, **config):
    return {
        "sourceId": source_id,
        "namespaceId": _NS_ID,
        "graphClusterClaim": _CLAIM_PK,
        "sourceSubType": sub_type,
        "queryable": queryable,
        "status": status or ("APPROVED" if queryable else "REGISTERED"),
        "configuration": json.dumps({"store": "neptune", "dataEndpoint": _HOST, **config}),
    }


def _translation(sparql=_SPARQL, *, valid=True, classes=None, steps=None):
    tbox = TBoxContext(classes=[{"uri": "http://example.org/o#Ticket"}] if classes is None else classes, properties=[])
    return TranslationResult(sparql=sparql, confidence=0.8, valid=valid, tbox_context=tbox, trace_steps=steps or [])


class _Harness:
    def __init__(self, translation=None, *, sources=None, sole=None, hits=None, bindings=None):
        self.translator = MagicMock(translate=AsyncMock(return_value=translation or _translation()))
        sources = sources if sources is not None else {"src-g1": _source()}
        self.registry = MagicMock()
        self.registry.get_source = AsyncMock(side_effect=lambda ns, sid: sources.get(sid, {}))
        self.registry.find_sole_graph_source = AsyncMock(return_value=sole)
        self.registry.get_graph_cluster_claim = AsyncMock(return_value={"PK": _CLAIM_PK, "namespaceId": _NS_ID})
        self.client = MagicMock()
        self.client.query = AsyncMock(
            return_value=bindings if bindings is not None else [{"ticket": "t1", "title": "x"}]
        )
        self.client.ask = AsyncMock(return_value=True)
        self.client.construct = AsyncMock(return_value=[{"subject": "s", "predicate": "p", "object": "o"}])
        self.client.close = AsyncMock()
        self.factory = MagicMock(return_value=self.client)
        self.vector = None
        if hits is not None:
            self.vector = MagicMock()
            self.vector.search = AsyncMock(side_effect=lambda *a, **k: hits if k.get("entity_type") == "class" else [])
        self.strategy = SparqlStrategy(
            translator=self.translator,
            sources_registry=self.registry,
            vector_client=self.vector,
            oss_ontology_index="ont" if hits is not None else "",
            client_cache=SparqlClientCache(factory=self.factory),
        )

    async def resolve(self, *, options=None, embedding=None, deadline=None, profile=None):
        self.trace = TraceCollector()
        ctx = StrategyContext(
            embedding=embedding, options=options or {}, trace=self.trace, deadline=deadline, profile=profile or {}
        )
        return await self.strategy.resolve("open tickets", _NS, ctx)

    def steps(self):
        return {s.step: s for s in self.trace.steps}


def _class_hit(uri, source_id, *, backend="RDF_SPARQL", score=0.9):
    return BaseVectorHit(
        id=uri,
        text=uri,
        score=score,
        metadata={"entity_type": "class", "entity_uri": uri, "data_source_id": source_id, "query_backend": backend},
    )


class TestContract:
    def test_name_and_backend(self):
        assert SparqlStrategy.name == StrategyOption.NL_TO_SPARQL
        assert SparqlStrategy.supported_backends == {StructuredBackend.RDF_SPARQL}


class TestHappyPath:
    async def test_executes_the_firewalled_query_against_the_sources_endpoint(self):
        h = _Harness(sole=_source())
        result = await h.resolve()
        assert result is not None
        assert result.strategy_name == StrategyOption.NL_TO_SPARQL
        assert result.sql == ""
        assert result.data_source_id == "src-g1"
        assert result.rows == [{"ticket": "t1", "title": "x"}]
        assert result.columns == ["ticket", "title"]
        executed = h.client.query.call_args.args[0]
        assert "LIMIT" in executed  # F3 applied before execution
        assert result.sparql == executed

    async def test_a_projected_variable_no_row_binds_keeps_its_column_in_projection_order(self):
        sparql = (
            "PREFIX ex: <http://example.org/o#>\n"
            "SELECT ?title ?ticket WHERE { ?ticket a ex:Ticket . OPTIONAL { ?ticket ex:title ?title } }"
        )
        h = _Harness(_translation(sparql), sole=_source(), bindings=[{"ticket": "t1"}])
        result = await h.resolve()
        assert result is not None
        assert result.columns == ["title", "ticket"]
        assert result.rows == [{"title": "", "ticket": "t1"}]

    async def test_the_client_is_built_from_the_sources_configuration(self):
        h = _Harness(sole=_source())
        await h.resolve()
        (config,) = h.factory.call_args.args
        assert config.host == _HOST

    async def test_a_named_graph_source_is_scoped_to_it(self):
        h = _Harness(sole=_source(dataNamedGraph=_G))
        await h.resolve()
        assert f"FROM <{_G}>" in h.client.query.call_args.args[0]

    async def test_max_results_bounds_the_query(self):
        h = _Harness(sole=_source())
        await h.resolve(options={"maxResults": 5})
        assert h.client.query.call_args.args[0].rstrip().endswith("LIMIT 5")

    async def test_ask_and_construct_return_rows(self):
        h = _Harness(_translation("ASK { ?s ?p ?o }"), sole=_source())
        assert (await h.resolve()).rows == [{"result": True}]
        h = _Harness(_translation("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"), sole=_source())
        result = await h.resolve()
        assert result.columns == ["subject", "predicate", "object"]

    async def test_construct_triples_are_capped_at_the_bound_and_marked_truncated(self):
        h = _Harness(_translation("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"), sole=_source())
        h.client.construct = AsyncMock(
            return_value=[{"subject": "s", "predicate": "p", "object": str(i)} for i in range(5)]
        )
        result = await h.resolve(options={"maxResults": 5})
        assert h.client.construct.call_args.kwargs["max_triples"] == 5
        assert result.truncated

    async def test_trace_uses_the_sparql_family(self):
        steps = [{"step": "t2.vkg.context", "status": "success"}, {"step": "t2.vkg.translate", "status": "success"}]
        h = _Harness(_translation(steps=steps), sole=_source())
        await h.resolve()
        names = set(h.steps())
        assert {"t2.sparql.context", "t2.sparql.translate", "t2.sparql.firewall", "t2.sparql.execute"} <= names
        assert not any(n.startswith("t2.vkg.") for n in names)
        assert h.steps()["t2.sparql.execute"].detail["strategy"] == "nl_to_sparql"


class TestRouting:
    async def test_a_pinned_source_wins(self):
        h = _Harness(sources={"src-pin": _source("src-pin")}, hits=[_class_hit("http://ex/T", "src-hit")])
        result = await h.resolve(options={"dataSourceId": "src-pin"}, embedding=[0.1])
        assert result.data_source_id == "src-pin"

    async def test_the_top_graph_class_hit_names_the_source(self):
        hits = [_class_hit("http://ex/T", "src-g2"), _class_hit("http://ex/U", "src-g1", score=0.5)]
        h = _Harness(sources={"src-g1": _source("src-g1"), "src-g2": _source("src-g2")}, hits=hits)
        assert (await h.resolve(embedding=[0.1])).data_source_id == "src-g2"
        h.registry.find_sole_graph_source.assert_not_awaited()

    async def test_retrieval_asks_for_graph_classes_only(self):
        h = _Harness(hits=[_class_hit("http://ex/T", "src-g1")])
        await h.resolve(embedding=[0.1])
        class_call = next(c for c in h.vector.search.call_args_list if c.kwargs.get("entity_type") == "class")
        assert class_call.kwargs["query_backend"] is StructuredBackend.RDF_SPARQL

    async def test_without_a_hit_the_sole_graph_source_is_used(self):
        h = _Harness(sole=_source("src-only"))
        assert (await h.resolve()).data_source_id == "src-only"

    async def test_without_a_hit_and_several_graph_sources_it_declines(self):
        h = _Harness(sole=None)
        assert await h.resolve() is None
        assert h.steps()["t2.sparql.failed"].detail["reason"] == "sparql_strategy_no_data_endpoint"
        h.client.query.assert_not_awaited()

    @pytest.mark.parametrize("pinned", [False, True], ids=["sole-source", "pinned-source"])
    async def test_an_unreadable_registry_is_traced_as_unavailable_not_as_no_source(self, pinned):
        h = _Harness(sole=_source(), sources={"src-g1": _source("src-g1")})
        h.registry.find_sole_graph_source.side_effect = RuntimeError("registry down")
        h.registry.get_source.side_effect = RuntimeError("registry down")
        options = {"dataSourceId": "src-g1"} if pinned else None
        assert await h.resolve(options=options) is None
        assert h.steps()["t2.sparql.failed"].detail["reason"] == "sparql_registry_unavailable"
        h.client.query.assert_not_awaited()

    async def test_a_pinned_sql_source_is_refused(self):
        h = _Harness(sources={"src-sql": _source("src-sql", sub_type="JDBC_DATABASE")})
        assert await h.resolve(options={"dataSourceId": "src-sql"}) is None
        h.factory.assert_not_called()

    async def test_an_unverified_source_is_refused(self):
        h = _Harness(sole=_source(queryable=False))
        assert await h.resolve() is None
        h.client.query.assert_not_awaited()

    async def test_a_registry_failure_declines(self):
        h = _Harness()
        h.registry.find_sole_graph_source.side_effect = RuntimeError("ddb down")
        assert await h.resolve() is None


class TestDeclines:
    async def test_no_graph_classes_is_reported_as_misconfigured(self):
        h = _Harness(_translation(classes=[]), sole=_source())
        assert await h.resolve() is None
        assert h.steps()["t2.sparql.failed"].detail["reason"] == "sparql_strategy_misconfigured"
        h.registry.find_sole_graph_source.assert_not_awaited()

    async def test_an_invalid_translation_declines_quietly(self):
        h = _Harness(_translation(sparql=None, valid=False), sole=_source())
        assert await h.resolve() is None
        assert "t2.sparql.failed" not in h.steps()

    @pytest.mark.parametrize("status", ["DELETING", "DELETE_FAILED"])
    async def test_a_source_on_its_way_out_is_never_queried(self, status):
        # A delete moves the row to DELETING without clearing queryable; it must stop answering at once.
        h = _Harness(sole=_source(status=status))
        assert await h.resolve() is None
        assert h.steps()["t2.sparql.failed"].detail["reason"] == "sparql_strategy_no_data_endpoint"
        h.factory.assert_not_called()

    async def test_a_source_being_re_verified_keeps_answering(self):
        # A re-verify takes the REGISTERED lock but leaves queryable as it was, on purpose.
        assert await _Harness(sole=_source(status="REGISTERED")).resolve() is not None

    async def test_an_unsafe_query_is_never_executed(self):
        h = _Harness(_translation("DELETE WHERE { ?s ?p ?o }"), sole=_source())
        assert await h.resolve() is None
        firewall = h.steps()["t2.sparql.firewall"]
        assert firewall.status == "error"
        assert firewall.detail["rule"] == "F1_read_only"
        h.factory.assert_not_called()

    async def test_a_query_reaching_another_graph_is_never_executed(self):
        sparql = "SELECT * WHERE { GRAPH <http://other> { ?s ?p ?o } }"
        h = _Harness(_translation(sparql), sole=_source(dataNamedGraph=_G))
        assert await h.resolve() is None
        h.client.query.assert_not_awaited()

    async def test_an_endpoint_error_is_recorded_and_declined(self):
        h = _Harness(sole=_source())
        h.client.query.side_effect = ConnectionError("unreachable")
        assert await h.resolve() is None
        execute = h.steps()["t2.sparql.execute"]
        assert execute.status == "error"
        assert execute.detail["reason"] == "sparql_strategy_exec_failed"

    async def test_execution_respects_the_request_deadline(self):
        h = _Harness(sole=_source())

        async def _slow(*a, **k):
            await asyncio.sleep(5)

        h.client.query.side_effect = _slow
        deadline = Deadline(budget_s=0.05)
        assert await h.resolve(deadline=deadline) is None
        assert h.steps()["t2.sparql.execute"].detail["error"] == "TimeoutError"

    async def test_an_unsupported_engine_declines(self):
        h = _Harness(sole=_source())
        from coa_serve.clients.sparql_clients import UnsupportedSparqlEngineError

        h.factory.side_effect = UnsupportedSparqlEngineError("no client")
        assert await h.resolve() is None
        assert h.steps()["t2.sparql.failed"].detail["reason"] == "sparql_client_build_failed"


class TestCallerGrant:
    """a grant whose tableAllowlist does not name the graph source gets no graph rows."""

    @pytest.mark.parametrize(
        "profile",
        [{"tableAllowlist": []}, {"tableAllowlist": ["orders"]}, {"tableAllowlist": "src-g1"}],
        ids=["deny-all", "other-tables", "malformed"],
    )
    async def test_a_grant_that_does_not_name_the_source_is_refused(self, profile):
        h = _Harness(sole=_source())
        assert await h.resolve(profile=profile) is None
        assert h.steps()["t2.sparql.failed"].detail["reason"] == "sparql_grant_restricted"
        h.factory.assert_not_called()
        h.client.query.assert_not_awaited()

    @pytest.mark.parametrize("profile", [{}, {"tableAllowlist": ["SRC-G1"]}], ids=["unrestricted", "names-the-source"])
    async def test_a_grant_that_permits_the_source_is_served(self, profile):
        h = _Harness(sole=_source())
        result = await h.resolve(profile=profile)
        assert result is not None and result.rows == [{"ticket": "t1", "title": "x"}]


class TestNamespaceAuthorization:
    """Cedar's namespace "query" decision gates the graph path as it gates the SQL path (a terminal 403)."""

    def _harness(self, denied):
        h = _Harness(sole=_source())
        gate = MagicMock()
        gate.authorize_namespace.return_value = FirewallResult(
            denied=denied, authorized_sql="", reason="Access denied by policy" if denied else None
        )
        h.strategy = SparqlStrategy(
            translator=h.translator,
            sources_registry=h.registry,
            client_cache=SparqlClientCache(factory=h.factory),
            namespace_authorizer=gate,
        )
        return h, gate

    async def test_a_principal_cedar_denies_gets_access_denied_before_any_work(self):
        h, gate = self._harness(denied=True)
        with pytest.raises(AccessDeniedError):
            await h.resolve(profile={"sub": "u1"})
        gate.authorize_namespace.assert_called_once_with(_NS, {"sub": "u1"})
        h.translator.translate.assert_not_awaited()
        h.client.query.assert_not_awaited()

    async def test_a_principal_cedar_admits_is_served(self):
        h, gate = self._harness(denied=False)
        assert await h.resolve() is not None
        gate.authorize_namespace.assert_called_once()


_TITLE = "http://example.org/o#title"


class TestColumnDenylist:
    """a grant's columnDenylist entry for the graph source names predicate IRIs the caller may not read."""

    async def test_a_query_reading_a_denied_predicate_returns_nothing(self):
        h = _Harness(sole=_source())
        assert await h.resolve(profile={"columnDenylist": {"SRC-G1": [_TITLE]}}) is None
        h.client.query.assert_not_awaited()

    async def test_a_query_with_a_variable_predicate_is_refused_while_a_predicate_is_denied(self):
        any_predicate = "SELECT ?ticket ?p ?o WHERE { ?ticket a <http://example.org/o#Ticket> ; ?p ?o }"
        h = _Harness(_translation(any_predicate), sole=_source())
        assert await h.resolve(profile={"columnDenylist": {"src-g1": [_TITLE]}}) is None
        h.client.query.assert_not_awaited()

    async def test_a_denied_name_that_is_not_an_iri_refuses_the_source(self):
        h = _Harness(sole=_source())
        assert await h.resolve(profile={"columnDenylist": {"src-g1": ["title"]}}) is None
        assert h.steps()["t2.sparql.failed"].detail["reason"] == "sparql_grant_restricted"
        h.client.query.assert_not_awaited()

    async def test_a_malformed_column_denylist_refuses_the_source(self):
        h = _Harness(sole=_source())
        assert await h.resolve(profile={"columnDenylist": {"src-g1": _TITLE}}) is None
        assert h.steps()["t2.sparql.failed"].detail["reason"] == "sparql_grant_restricted"

    @pytest.mark.parametrize(
        "denied",
        [["ex:title"], [_TITLE + "\n"], ["title"], ["ftp://example.org/o#title"]],
        ids=["curie", "trailing-newline", "bare-name", "other-scheme"],
    )
    async def test_a_denied_name_that_is_not_a_full_http_or_urn_iri_refuses_the_source(self, denied):
        h = _Harness(sole=_source())
        assert await h.resolve(profile={"columnDenylist": {"src-g1": denied}}) is None
        assert h.steps()["t2.sparql.failed"].detail["reason"] == "sparql_grant_restricted"
        h.client.query.assert_not_awaited()

    @pytest.mark.parametrize("key", ["Orders Graph", "orders graph", "*"], ids=["name", "name-lowercase", "wildcard"])
    async def test_an_iri_entry_keyed_by_the_source_name_or_a_wildcard_refuses_the_source(self, key):
        # The key is the source id; an entry under the name or "*" would otherwise be silently ignored.
        source = _source()
        source["name"] = "Orders Graph"
        h = _Harness(sole=source)
        assert await h.resolve(profile={"columnDenylist": {key: [_TITLE]}}) is None
        assert h.steps()["t2.sparql.failed"].detail["reason"] == "sparql_grant_restricted"
        h.client.query.assert_not_awaited()

    async def test_an_iri_entry_for_another_source_does_not_refuse_this_one(self):
        name_only = "SELECT ?ticket WHERE { ?ticket a <http://example.org/o#Ticket> }"
        h = _Harness(_translation(name_only), sole=_source())
        assert await h.resolve(profile={"columnDenylist": {"src-other": [_TITLE]}}) is not None

    async def test_denied_predicates_are_kept_out_of_the_schema_the_llm_sees(self):
        h = _Harness(sole=_source())
        await h.resolve(profile={"columnDenylist": {"src-g1": [_TITLE], "orders": ["ssn"]}})
        assert h.translator.translate.call_args.kwargs["exclude_properties"] == frozenset({_TITLE})

    async def test_a_query_avoiding_the_denied_predicate_is_served(self):
        name_only = "SELECT ?ticket WHERE { ?ticket a <http://example.org/o#Ticket> }"
        h = _Harness(_translation(name_only), sole=_source())
        assert await h.resolve(profile={"columnDenylist": {"src-g1": [_TITLE]}}) is not None

    @pytest.mark.parametrize(
        "profile",
        [{"columnDenylist": {"orders": ["ssn"]}}, {"columnDenylist": {"src-g1": []}}, {"columnDenylist": {}}],
        ids=["another-table", "empty-entry", "empty-map"],
    )
    async def test_a_denylist_that_names_nothing_of_the_source_changes_nothing(self, profile):
        h = _Harness(sole=_source())
        result = await h.resolve(profile=profile)
        assert result is not None and result.rows == [{"ticket": "t1", "title": "x"}]
        assert "exclude_properties" not in h.translator.translate.call_args.kwargs or not (
            h.translator.translate.call_args.kwargs["exclude_properties"] & {_TITLE}
        )


class TestClusterClaim:
    """a source is read only while its namespace holds the claim on its Neptune cluster; anything else fails closed."""

    async def test_a_source_whose_namespace_holds_the_claim_is_served(self):
        h = _Harness(sole=_source())
        assert await h.resolve() is not None
        h.registry.get_graph_cluster_claim.assert_awaited_once_with(_CLAIM_PK)

    @pytest.mark.parametrize(
        "claim", [{}, {"PK": _CLAIM_PK, "namespaceId": "another-ns"}], ids=["unclaimed", "claimed-elsewhere"]
    )
    async def test_a_cluster_not_claimed_by_the_sources_namespace_is_never_queried(self, claim):
        h = _Harness(sole=_source())
        h.registry.get_graph_cluster_claim.return_value = claim
        assert await h.resolve() is None
        assert h.steps()["t2.sparql.failed"].detail["reason"] == "sparql_strategy_no_data_endpoint"
        h.factory.assert_not_called()

    async def test_a_source_that_notes_no_claim_is_never_queried(self):
        item = _source()
        del item["graphClusterClaim"]
        h = _Harness(sole=item)
        assert await h.resolve() is None
        h.factory.assert_not_called()
        h.registry.get_graph_cluster_claim.assert_not_awaited()

    async def test_a_failed_claim_lookup_is_never_queried(self):
        h = _Harness(sole=_source())
        h.registry.get_graph_cluster_claim.side_effect = RuntimeError("ddb down")
        assert await h.resolve() is None
        h.factory.assert_not_called()


class TestBindingsToRows:
    def test_columns_follow_the_projection_and_unbound_is_empty(self):
        rows, columns = _bindings_to_rows([{"b": "1"}, {"a": "2", "b": "3"}], ("a", "b"))
        assert columns == ["a", "b"]
        assert rows == [{"a": "", "b": "1"}, {"a": "2", "b": "3"}]

    def test_unprojected_variables_are_appended(self):
        _, columns = _bindings_to_rows([{"x": "1", "y": "2"}], ())
        assert columns == ["x", "y"]


class TestNoLeakOnEndpointFailure:
    """MAIN_CATCHUP_SEC MC-1: a SPARQL endpoint failure reaches every caller's trace without its error text.

    The httpx message names the cluster endpoint URL (and can carry more). The trace step gets a generic message
    (client_error_detail: the exception type) plus a correlation id; the text goes to the log only, with the same id.
    """

    async def test_the_trace_step_carries_no_error_text_only_a_correlation_id(self, monkeypatch):
        import coa_serve.tier2.sparql.strategy as strategy_mod

        logged: list[dict] = []
        monkeypatch.setattr(strategy_mod.logger, "warning", lambda event, **kw: logged.append({"event": event, **kw}))
        endpoint = "https://coa-dev-graph-source.cluster-examplehash1.us-west-2.neptune.amazonaws.com:8182/sparql"
        h = _Harness(sole=_source())
        h.client.query.side_effect = ConnectionError(f"All connection attempts failed: {endpoint}")
        assert await h.resolve() is None
        detail = h.steps()["t2.sparql.execute"].detail
        assert endpoint not in json.dumps(detail) and "message" not in detail
        assert detail["error"] == "ConnectionError" and detail["reason"] == "sparql_strategy_exec_failed"
        ref = detail["errorRef"]
        assert isinstance(ref, str) and len(ref) >= 8
        [entry] = [e for e in logged if e["event"] == "sparql_strategy_exec_failed"]
        assert entry["error_ref"] == ref and endpoint in entry["message"]


class TestPlatformCluster:
    """a source on COA's own catalog cluster is never queried, even when a record says it is verified."""

    @pytest.mark.parametrize("host", _PLATFORM_HOSTS, ids=["writer", "reader", "custom"])
    async def test_a_verified_source_on_the_platform_cluster_is_never_queried(self, host):
        # A custom endpoint names no cluster: verify keyed its claim on the cluster it resolved, here the platform's.
        source = _source(dataEndpoint=host)
        source["graphClusterClaim"] = "GRAPHCLUSTER#us-east-1#coa-platform"
        h = _Harness(sole=source)
        h.registry.get_graph_cluster_claim.return_value = {"PK": source["graphClusterClaim"], "namespaceId": _NS_ID}
        assert await h.resolve() is None
        h.factory.assert_not_called()
        h.client.query.assert_not_awaited()

    async def test_a_custom_endpoint_of_another_cluster_in_the_platforms_account_is_served(self):
        # Neptune's endpoint hash ("abc") is the same for every cluster of an account and region.
        h = _Harness(sole=_source(dataEndpoint="reporting.cluster-custom-abc.us-east-1.neptune.amazonaws.com"))
        assert await h.resolve() is not None
        h.client.query.assert_awaited()

    async def test_without_a_named_platform_cluster_no_source_is_queried(self, monkeypatch):
        monkeypatch.delenv("NEPTUNE_ENDPOINT", raising=False)
        h = _Harness(sole=_source())
        assert await h.resolve() is None
        h.factory.assert_not_called()

    async def test_a_source_on_another_cluster_is_still_queried(self):
        h = _Harness(sole=_source())
        assert await h.resolve() is not None
        h.client.query.assert_awaited()


class TestWorkBound:
    async def test_a_neptune_source_query_carries_the_server_timeout(self, monkeypatch):
        monkeypatch.setenv("NEPTUNE_TIMEOUT", "8")
        h = _Harness(sole=_source())
        assert await h.resolve() is not None
        executed = h.client.query.call_args.args[0]
        assert "<http://aws.amazon.com/neptune/vocab/v01/QueryHints#queryTimeout> 7000" in executed
