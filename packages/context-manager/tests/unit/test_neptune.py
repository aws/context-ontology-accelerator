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

    def test_missing_endpoint_raises(self):
        from coa_serve.clients.neptune import NeptuneGraphClient

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
