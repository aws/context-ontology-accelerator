# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Neptune GraphClient."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.unit
class TestNeptuneGraphClient:
    async def test_query_parses_sparql_json_results(self):
        from coa_serve.clients.neptune import NeptuneGraphClient

        client = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": {
                "bindings": [
                    {
                        "class": {"type": "uri", "value": "http://ex.org/Claim"},
                        "label": {"type": "literal", "value": "Claim"},
                    },
                    {
                        "class": {"type": "uri", "value": "http://ex.org/Customer"},
                        "label": {"type": "literal", "value": "Customer"},
                    },
                ]
            }
        }

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        client._http = mock_http

        results = await client.query("SELECT ?class ?label WHERE { ?class a owl:Class }")

        assert len(results) == 2
        assert results[0]["class"] == "http://ex.org/Claim"
        assert results[1]["label"] == "Customer"

    async def test_query_handles_string_values_defensively(self):
        """Neptune can return plain strings instead of {value, type} dicts in some cases."""
        from coa_serve.clients.neptune import NeptuneGraphClient

        client = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": {
                "bindings": [
                    {
                        "name": {"type": "literal", "value": "metric_a"},
                        "raw_field": "already_a_string",
                    },
                ]
            }
        }

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        client._http = mock_http

        results = await client.query("SELECT ?name ?raw_field WHERE { ?s ?p ?o }")

        assert len(results) == 1
        assert results[0]["name"] == "metric_a"
        assert results[0]["raw_field"] == "already_a_string"

    async def test_ask_returns_boolean(self):
        from coa_serve.clients.neptune import NeptuneGraphClient

        client = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"boolean": True}

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        client._http = mock_http

        result = await client.ask("ASK { <http://ex.org/Claim> ?p ?o }")
        assert result is True

    async def test_health_check_returns_status(self):
        from coa_serve.clients.neptune import NeptuneGraphClient

        client = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_http = AsyncMock()
        mock_http.get.return_value = mock_response
        client._http = mock_http

        result = await client.health_check()
        assert result["status"] == "ok"

    async def test_health_check_returns_error_on_exception(self):
        from coa_serve.clients.neptune import NeptuneGraphClient

        client = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")
        mock_http = AsyncMock()
        mock_http.get.side_effect = ConnectionError("connection refused")
        client._http = mock_http

        result = await client.health_check()
        assert result["status"] == "error"
        assert "connection refused" in result["detail"]

    async def test_query_raises_on_http_error(self):
        import httpx
        from coa_serve.clients.neptune import NeptuneGraphClient

        client = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Internal Server Error",
            request=MagicMock(),
            response=mock_response,
        )

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        client._http = mock_http

        with pytest.raises(httpx.HTTPStatusError):
            await client.query("SELECT ?x WHERE { ?x a owl:Class }")

    async def test_close_cleans_up_http_client(self):
        from coa_serve.clients.neptune import NeptuneGraphClient

        client = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")
        mock_http = AsyncMock()
        client._http = mock_http

        await client.close()
        mock_http.aclose.assert_called_once()
        assert client._http is None

    async def test_a_closed_client_never_builds_a_new_pool(self):
        # A retry in flight when the client is closed (serve shutdown, a dropped cache entry) used to open a fresh
        # httpx pool that nothing ever closed.
        from coa_serve.clients.neptune import NeptuneGraphClient

        client = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")
        await client.close()
        with pytest.raises(RuntimeError, match="closed"):
            await client._get_http()
        assert client._http is None

    def test_missing_endpoint_raises(self, monkeypatch):
        from coa_serve.clients.neptune import NeptuneGraphClient

        monkeypatch.delenv("NEPTUNE_ENDPOINT", raising=False)
        with pytest.raises(ValueError, match="Neptune endpoint must be provided"):
            NeptuneGraphClient(endpoint="")

    async def test_rejects_write_sparql(self):
        from coa_serve.clients.neptune import NeptuneGraphClient, SPARQLValidationError

        client = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")
        with pytest.raises(SPARQLValidationError):
            await client.query("INSERT DATA { <s> <p> <o> }")

        with pytest.raises(SPARQLValidationError):
            await client.query("DELETE WHERE { ?s ?p ?o }")

        with pytest.raises(SPARQLValidationError):
            await client.ask("DROP GRAPH <http://example.org/g>")

    @pytest.mark.parametrize(
        "sparql",
        [
            # Class IRI whose host contains a write keyword ("clear").
            "SELECT ?p WHERE { ?p <http://www.w3.org/2000/01/rdf-schema#domain> <https://clear.example/ns#Account> }",
            "PREFIX b: <https://load.example/add#> SELECT ?c WHERE { ?c a b:Thing }",
            'SELECT ?c WHERE { ?c <http://www.w3.org/2000/01/rdf-schema#label> "Load Balancer" }',
            "SELECT ?c WHERE { ?c ?p '''multi\nline Delete note''' }",
            "SELECT ?c WHERE { ?c ?p ?o } # drop this comment",
        ],
    )
    def test_keyword_inside_iri_literal_or_comment_allowed(self, sparql):
        from coa_serve.clients.neptune import NeptuneGraphClient

        NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")._validate_sparql(sparql)

    @pytest.mark.parametrize(
        "sparql",
        [
            'SELECT ?c WHERE { ?c ?p "x" } ; CLEAR ALL',
            "PREFIX b: <https://clear.example/ns#> INSERT DATA { b:s b:p b:o }",
            # A keyword after an unterminated quote is not hidden.
            'SELECT ?c WHERE { ?c ?p "unterminated\n} ; DROP ALL',
            # A quote inside an IRI does not open a literal that swallows the write.
            'SELECT * WHERE { <http://x/a"b> ?p ?o } ; LOAD <http://x/g> "',
            # SPARQL ends a comment at CR as well as LF.
            "# hi\rDROP ALL",
        ],
    )
    def test_write_outside_data_tokens_still_rejected(self, sparql):
        from coa_serve.clients.neptune import NeptuneGraphClient, SPARQLValidationError

        with pytest.raises(SPARQLValidationError):
            NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")._validate_sparql(sparql)

    async def test_injects_limit_when_missing(self):
        from coa_serve.clients.neptune import NeptuneGraphClient

        client = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"results": {"bindings": []}}

        mock_http = AsyncMock()
        mock_http.post.return_value = mock_response
        client._http = mock_http

        await client.query("SELECT ?x WHERE { ?x a owl:Class }", max_results=100)

        call_kwargs = mock_http.post.call_args[1]
        posted_body = call_kwargs["content"]
        assert "LIMIT+100" in posted_body or "LIMIT%20100" in posted_body or "LIMIT 100" in posted_body


@pytest.mark.unit
class TestNeptuneDataEndpointClient:
    """The per-source data client: transport only, gated upstream by SPARQLFirewall."""

    def _client(self, **kwargs):
        from coa_serve.clients.neptune import NeptuneGraphClient

        client = NeptuneGraphClient(endpoint="data.neptune.amazonaws.com", **kwargs)
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"results": {"bindings": []}}
        response.text = (
            "<http://ex.org/t1> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://ex.org/Ticket> .\n"
            '<http://ex.org/t1> <http://ex.org/title> "Load balancer down" .\n'
        )
        client._http = AsyncMock()
        client._http.post.return_value = response
        return client

    async def test_a_read_with_a_keyword_outside_data_passes_only_when_the_guard_is_off(self):
        from coa_serve.clients.neptune import SPARQLValidationError

        # MCM-2: a keyword inside a literal is blanked even with the guard on, so this witness uses a keyword-named
        # variable, which the guard refuses; only the opt-out lets this legitimate read through.
        sparql = "SELECT ?load WHERE { ?s <http://ex.org/p> ?load } LIMIT 10"
        with pytest.raises(SPARQLValidationError):
            await self._client().query(sparql)
        client = self._client(enforce_read_only=False)
        await client.query(sparql)
        client._http.post.assert_awaited_once()

    async def test_the_shared_client_keeps_its_keyword_guard(self):
        from coa_serve.clients.neptune import SPARQLValidationError

        # main blanks literals, IRIs and comments before the keyword check, so a literal "load" is data, not an
        # update; the guard itself stays on the shared client and still refuses a real SPARQL Update.
        with pytest.raises(SPARQLValidationError):
            await self._client().query("LOAD <http://ex.org/data.ttl> INTO GRAPH <http://ex.org/g>")
        with pytest.raises(SPARQLValidationError):
            await self._client().query('INSERT DATA { <http://ex.org/a> <http://ex.org/p> "load" }')

    async def test_construct_requests_ntriples_and_returns_triples_as_rows(self):
        client = self._client(enforce_read_only=False)
        rows = await client.construct("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }")
        assert client._http.post.call_args.kwargs["headers"]["Accept"] == "application/n-triples"
        assert {
            "subject": "http://ex.org/t1",
            "predicate": "http://ex.org/title",
            "object": "Load balancer down",
        } in rows
        assert len(rows) == 2

    async def test_construct_orders_triples_whose_objects_do_not_compare(self):
        # A NaN double and a decimal under one subject and predicate.
        xsd = "http://www.w3.org/2001/XMLSchema#"
        client = self._client(enforce_read_only=False)
        client._http.post.return_value.text = (
            f'<http://ex.org/s> <http://ex.org/p> "NaN"^^<{xsd}double> .\n'
            f'<http://ex.org/s> <http://ex.org/p> "1.5"^^<{xsd}decimal> .\n'
        )
        rows = await client.construct("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }", max_triples=1)
        assert len(rows) == 1

    async def test_construct_caps_the_triples_it_returns(self):
        client = self._client(enforce_read_only=False)
        rows = await client.construct("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }", max_triples=1)
        assert rows == [
            {"subject": "http://ex.org/t1", "predicate": "http://ex.org/title", "object": "Load balancer down"}
        ]
