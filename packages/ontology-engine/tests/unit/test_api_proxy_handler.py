# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for api_proxy_handler — Lambda proxy that forwards to ECS."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "http://ontology-engine.ns:8001")
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://app.example.com")


@pytest.fixture()
def mod():
    from coa_ontology import api_proxy_handler

    return api_proxy_handler


class TestPathMapping:
    def test_induce_path(self, mod):
        assert mod._PATH_MAP["/namespaces/{namespaceId}/induce"] == "/induce/"

    def test_proposals_path(self, mod):
        assert mod._PATH_MAP["/namespaces/{namespaceId}/proposals"] == "/ontology/proposals/"

    def test_graph_search_path(self, mod):
        assert mod._PATH_MAP["/namespaces/{namespaceId}/graph/search"] == "/graph/search"


class TestSchemaQueryTranslation:
    """MCP's describe_schema spells its query params in camelCase; FastAPI binds
    snake_case. Unrenamed they are forwarded, ignored, and replaced by the route
    defaults — a silent wrong answer rather than an error.
    """

    @pytest.fixture()
    def forward(self, mod):
        """Run the handler against a stubbed ECS call and return the forwarded URL."""

        def _run(event: dict) -> str:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b"{}"
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                assert mod.handler(event, None)["statusCode"] == 200
                return mock_urlopen.call_args[0][0].full_url

        return _run

    def _event(self, **query) -> dict:
        return {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/graph/schema",
            "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111"},
            "queryStringParameters": query or None,
            "body": "",
        }

    def test_max_results_renamed(self, forward):
        url = forward(self._event(maxResults="500"))
        assert "max_results=500" in url
        assert "maxResults" not in url

    def test_include_properties_renamed(self, forward):
        url = forward(self._event(includeProperties="false"))
        assert "include_properties=false" in url

    def test_namespace_still_threaded(self, forward):
        assert "namespace=11111111-1111-4111-8111-111111111111" in forward(self._event(maxResults="10"))

    def test_empty_value_dropped(self, forward):
        """API Gateway supplies "" for a valueless param; forwarding it would
        override the route default with a blank and 422."""
        url = forward(self._event(maxResults=""))
        assert "max_results" not in url

    def test_aliases_do_not_leak_to_other_routes(self, forward):
        """The rename is scoped per backend path — a route that genuinely takes a
        camelCase param must keep receiving it verbatim."""
        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/graph/search",
            "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111"},
            "queryStringParameters": {"maxResults": "5"},
            "body": "",
        }
        assert "maxResults=5" in forward(event)


class TestHandler:
    @patch("urllib.request.urlopen")
    def test_successful_forward(self, mock_urlopen, mod):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"result": "ok"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = {
            "httpMethod": "POST",
            "resource": "/namespaces/{namespaceId}/induce",
            "pathParameters": {"namespaceId": "12312312-3123-4123-8123-123123123123"},
            "queryStringParameters": None,
            "body": '{"datasourceId": "ds-1"}',
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        assert json.loads(result["body"]) == {"result": "ok"}
        call_args = mock_urlopen.call_args[0][0]
        assert "http://ontology-engine.ns:8001/induce/" in call_args.full_url
        assert "namespace=12312312-3123-4123-8123-123123123123" in call_args.full_url

    @patch("urllib.request.urlopen")
    def test_path_param_substitution(self, mock_urlopen, mod):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"id": "p-1"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/proposals/{proposalId}",
            "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111", "proposalId": "p-1"},
            "queryStringParameters": {},
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        call_url = mock_urlopen.call_args[0][0].full_url
        assert "/ontology/proposals/p-1" in call_url
        assert "namespace=11111111-1111-4111-8111-111111111111" in call_url

    @patch("urllib.request.urlopen")
    def test_path_param_cannot_smuggle_namespace(self, mock_urlopen, mod):
        """A '?' or '#' in a path-param value must stay trapped in its path
        segment. If a bare '?' reached the backend URL it would start a query
        string, demoting the authorizer-threaded ``?namespace=own-ns`` and
        letting a caller reach another tenant's data. The proxy percent-encodes
        it, so the namespace stays authoritative. (This guards the hostile case
        even though real clients pre-encode the id.)
        """
        from urllib.parse import parse_qs, urlparse

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"{}"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = {
            "httpMethod": "DELETE",
            "resource": "/namespaces/{namespaceId}/ontologies/{ontologyId}",
            "pathParameters": {
                "namespaceId": "33333333-3333-4333-8333-333333333333",
                "ontologyId": "abc?namespace=victim-ns&z=",
            },
            "queryStringParameters": None,
            "body": "",
        }
        assert mod.handler(event, None)["statusCode"] == 200

        parsed = urlparse(mock_urlopen.call_args[0][0].full_url)
        # The authorized namespace wins; the smuggled one never reaches the query.
        assert parse_qs(parsed.query)["namespace"][0] == "33333333-3333-4333-8333-333333333333"
        assert "victim-ns" not in parsed.query
        # The smuggling '?' is percent-encoded and stays inside the path segment.
        assert "abc%3Fnamespace" in parsed.path

    @pytest.mark.parametrize(
        "ontology_id",
        [
            "https://schema.org/",  # the real foundational-ontology id (trailing slash)
            "http://example.org/onto#Customer",  # OWL IRI with ':' '/' '#'
            "catalog/customers/v2",  # slash-bearing identifier
            "オントロジー商品マスタ",  # non-ASCII (Japanese)
            "produkt-stammdaten-größe",  # non-ASCII (German)
        ],
    )
    @patch("urllib.request.urlopen")
    def test_iri_path_param_round_trips_through_backend(self, mock_urlopen, mod, ontology_id):
        """ontologyId is a free-form IRI/slash/non-ASCII identifier. The backend
        keys the ontology's named graph on ``quote(raw_id, safe="")`` on both the
        write (ingest) and read (download) paths, so the proxy must deliver a
        segment the backend decodes back to the RAW id. Double-encoding it makes
        write-URI != read-URI and the download reads an empty graph — the real
        e2e failure (0 of N triples). API Gateway hands the proxy the id still
        encoded, so the invariant is: a single unquote of the emitted segment
        recovers the raw id.
        """
        from urllib.parse import parse_qs, quote, unquote, urlsplit

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"{}"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        # The writer keys the named graph on quote(raw_id, safe=""); API Gateway
        # delivers the id to the proxy still encoded (client sent quote(id)).
        write_graph_segment = quote(ontology_id, safe="")
        delivered_encoded = quote(ontology_id, safe="")

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/ontologies/{ontologyId}/download",
            "pathParameters": {"namespaceId": "33333333-3333-4333-8333-333333333333", "ontologyId": delivered_encoded},
            "queryStringParameters": None,
            "body": "",
        }
        assert mod.handler(event, None)["statusCode"] == 200

        proxied = urlsplit(mock_urlopen.call_args[0][0].full_url)
        emitted_segment = proxied.path.split("/ontologies/", 1)[1].rsplit("/download", 1)[0]

        # The decisive, transport-independent property: a single unquote of the
        # emitted segment must recover the raw id (the backend decodes once, then
        # re-encodes to rebuild the graph URI). Both shipped-broken variants
        # (safe="" and safe="/:" without the unquote) emit a double-encoded
        # segment -> read URI %253A != write URI %3A -> empty graph. Asserting on
        # the emitted bytes avoids an HTTP client's decode leniency hiding it.
        assert unquote(emitted_segment) == ontology_id, (
            f"proxy emitted {emitted_segment!r}; one backend decode gives "
            f"{unquote(emitted_segment)!r}, not the raw id {ontology_id!r} — the "
            f"download would recompute a differently-encoded (empty) named graph"
        )
        # And re-encoding that recovered id reproduces the writer's graph segment.
        assert quote(unquote(emitted_segment), safe="") == write_graph_segment

        # Namespace stays authoritative regardless of the id's contents.
        assert parse_qs(proxied.query)["namespace"][0] == "33333333-3333-4333-8333-333333333333"

    @patch("urllib.request.urlopen")
    def test_http_error_forwarded(self, mock_urlopen, mod):
        from urllib.error import HTTPError

        err = HTTPError(url="http://test", code=404, msg="Not Found", hdrs={}, fp=None)
        mock_urlopen.side_effect = err

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/ontologies",
            "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111"},
            "queryStringParameters": None,
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 404

    @patch("urllib.request.urlopen")
    def test_url_error_returns_502(self, mock_urlopen, mod):
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused")

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/ontologies",
            "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111"},
            "queryStringParameters": None,
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 502
        assert json.loads(result["body"]) == {"message": mod._MSG_BACKEND_UNREACHABLE}
        assert "Connection refused" not in result["body"]

    @patch("urllib.request.urlopen")
    def test_http_error_without_body_gets_a_fixed_message(self, mock_urlopen, mod):
        from urllib.error import HTTPError

        mock_urlopen.side_effect = HTTPError("http://10.0.0.7:8001/x", 503, "Svc Unavailable", {}, None)
        result = mod.handler(self._ontologies_event(), None)

        assert result["statusCode"] == 503
        assert json.loads(result["body"]) == {"message": mod._MSG_BACKEND_ERROR}
        assert "10.0.0.7" not in result["body"]

    @patch("urllib.request.urlopen")
    def test_http_error_body_from_the_engine_passes_through(self, mock_urlopen, mod):
        import io
        from urllib.error import HTTPError

        engine_body = b'{"detail": "Proposal not found"}'
        mock_urlopen.side_effect = HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(engine_body))
        result = mod.handler(self._ontologies_event(), None)

        assert (result["statusCode"], result["body"]) == (404, engine_body.decode())

    @pytest.mark.parametrize(
        "error",
        [TimeoutError("timed out"), URLError(TimeoutError("timed out"))],
        ids=["read-timeout", "connect-timeout"],
    )
    @patch("urllib.request.urlopen")
    def test_backend_timeout_returns_504(self, mock_urlopen, mod, error):
        mock_urlopen.side_effect = error
        result = mod.handler(self._ontologies_event(), None)

        assert result["statusCode"] == 504
        assert json.loads(result["body"]) == {"message": mod._MSG_BACKEND_TIMEOUT}

    @patch("urllib.request.urlopen")
    def test_backend_timeout_is_under_the_api_gateway_limit(self, mock_urlopen, mod):
        """An ordinary route answers just before API Gateway (29 s) and the 30 s
        direct Lambda callers give up, so they see this handler's 504."""
        mock_urlopen.side_effect = TimeoutError()
        mod.handler(self._ontologies_event(), None)
        assert mock_urlopen.call_args.kwargs["timeout"] == mod._BACKEND_TIMEOUT_SECONDS == 28

    @patch("urllib.request.urlopen")
    def test_idempotent_post_uses_the_tighter_backend_timeout(self, mock_urlopen, mod):
        """A ``clientToken`` POST times out sooner, so the idempotency wrapper
        releases its claim well before the Lambda itself is killed. With no
        clientToken the wrapper forwards directly, through the same timeout."""
        mock_urlopen.side_effect = TimeoutError()
        event = {
            "httpMethod": "POST",
            "resource": "/namespaces/{namespaceId}/induce",
            "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111"},
            "queryStringParameters": None,
            "headers": {"Content-Type": "application/json"},
            "body": "{}",
        }
        result = mod.handler(event, None)
        assert result["statusCode"] == 504
        assert mock_urlopen.call_args.kwargs["timeout"] == mod._IDEMPOTENT_BACKEND_TIMEOUT_SECONDS == 25

    @staticmethod
    def _ontologies_event() -> dict:
        return {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/ontologies",
            "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111"},
            "queryStringParameters": None,
            "body": "",
        }

    @patch("urllib.request.urlopen")
    def test_query_params_forwarded(self, mock_urlopen, mod):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/graph/search",
            "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111"},
            "queryStringParameters": {"q": "Customer", "limit": "10"},
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        call_url = mock_urlopen.call_args[0][0].full_url
        assert "q=Customer" in call_url
        assert "limit=10" in call_url
        assert "namespace=11111111-1111-4111-8111-111111111111" in call_url


class TestSystemHealth:
    """Test /system-health endpoint security — no infrastructure details leak on error."""

    @patch("boto3.client")
    @patch("coa_ontology.api_proxy_handler.resolve_region", return_value="us-east-1")
    def test_dynamodb_error_returns_opaque_message(self, mock_region, mock_boto_client, mod, monkeypatch):
        """DynamoDB ClientError with ARN must not leak to response body."""
        from botocore.exceptions import ClientError

        error_resp = {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": (
                    "User: arn:aws:iam::123456789012:assumed-role/OntologyLambdaRole/session "
                    "is not authorized to perform: dynamodb:DescribeTable on resource: "
                    "arn:aws:dynamodb:us-east-1:123456789012:table/semantic-sources-prod"
                ),
            }
        }
        ddb_client = MagicMock()
        ddb_client.describe_table.side_effect = ClientError(error_resp, "DescribeTable")
        mock_boto_client.return_value = ddb_client

        monkeypatch.setenv("SOURCES_TABLE", "semantic-sources-prod")
        monkeypatch.setenv("NEPTUNE_ENDPOINT", "")
        monkeypatch.setenv("OPENSEARCH_ENDPOINT", "")

        event = {"httpMethod": "GET", "resource": "/system-health"}
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["dynamodb"] == "error"
        assert "123456789012" not in result["body"]
        assert "arn:aws" not in result["body"]
        assert "semantic-sources-prod" not in result["body"]

    @patch("boto3.client")
    @patch("coa_ontology.api_proxy_handler.resolve_region", return_value="us-east-1")
    def test_neptune_error_returns_opaque_message(self, mock_region, mock_boto_client, mod, monkeypatch):
        """Neptune connectivity failure must not leak endpoint hostname."""
        neptune_client = MagicMock()
        neptune_client.execute_open_cypher_query.side_effect = Exception(
            "Could not connect to neptune-cluster-abc123.us-east-1.neptune.amazonaws.com:8182"
        )

        def client_factory(service, **kwargs):
            if service == "neptunedata":
                return neptune_client
            return MagicMock()

        mock_boto_client.side_effect = client_factory

        monkeypatch.setenv("SOURCES_TABLE", "")
        monkeypatch.setenv("NEPTUNE_ENDPOINT", "neptune-cluster-abc123.us-east-1.neptune.amazonaws.com")
        monkeypatch.setenv("OPENSEARCH_ENDPOINT", "")

        event = {"httpMethod": "GET", "resource": "/system-health"}
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["neptuneKg"] == "error"
        assert "neptune-cluster-abc123" not in result["body"]
        assert ".neptune.amazonaws.com" not in result["body"]

    @patch("boto3.client")
    @patch("coa_ontology.api_proxy_handler.resolve_region", return_value="us-east-1")
    def test_opensearch_error_returns_opaque_message(self, mock_region, mock_boto_client, mod, monkeypatch):
        """AOSS batch_get_collection failure must not leak cluster ID."""
        from botocore.exceptions import ClientError

        error_resp = {
            "Error": {
                "Code": "ValidationException",
                "Message": "Collection xyz789 does not exist in account 123456789012",
            }
        }
        aoss_client = MagicMock()
        aoss_client.batch_get_collection.side_effect = ClientError(error_resp, "BatchGetCollection")
        mock_boto_client.return_value = aoss_client

        monkeypatch.setenv("SOURCES_TABLE", "")
        monkeypatch.setenv("NEPTUNE_ENDPOINT", "")
        monkeypatch.setenv("OPENSEARCH_ENDPOINT", "https://xyz789.us-east-1.aoss.amazonaws.com")

        event = {"httpMethod": "GET", "resource": "/system-health"}
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["opensearch"] == "error"
        assert "xyz789" not in result["body"]
        assert "123456789012" not in result["body"]

    @patch("urllib.request.urlopen")
    @patch("coa_ontology.api_proxy_handler.resolve_region", return_value="us-east-1")
    def test_ontology_induction_readiness_error_opaque(self, mock_region, mock_urlopen, mod, monkeypatch):
        """Ontology-engine /readiness failure must not leak internal endpoint."""
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused to http://ontology-engine.internal:8001/readiness")

        monkeypatch.setenv("SOURCES_TABLE", "")
        monkeypatch.setenv("NEPTUNE_ENDPOINT", "")
        monkeypatch.setenv("OPENSEARCH_ENDPOINT", "")

        event = {"httpMethod": "GET", "resource": "/system-health"}
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        ont_ind = body["ontologyInduction"]
        assert ont_ind["graphStore"] == "error"
        assert "ontology-engine.internal" not in result["body"]
        assert "8001" not in result["body"]


class TestCorsHeaders:
    """Test that CORS headers are set from ALLOWED_ORIGIN environment variable."""

    @patch("urllib.request.urlopen")
    def test_cors_header_in_success_response(self, mock_urlopen, mod):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"result": "ok"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/ontologies",
            "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111"},
            "queryStringParameters": None,
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://app.example.com"

    @patch("urllib.request.urlopen")
    def test_cors_header_in_http_error_response(self, mock_urlopen, mod):
        from urllib.error import HTTPError

        err = HTTPError(url="http://test", code=404, msg="Not Found", hdrs={}, fp=None)
        mock_urlopen.side_effect = err

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/ontologies",
            "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111"},
            "queryStringParameters": None,
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 404
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://app.example.com"

    @patch("urllib.request.urlopen")
    def test_cors_header_in_url_error_response(self, mock_urlopen, mod):
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused")

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/ontologies",
            "pathParameters": {"namespaceId": "11111111-1111-4111-8111-111111111111"},
            "queryStringParameters": None,
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 502
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://app.example.com"

    def test_cors_header_in_system_health_response(self, mod):
        """Test that system-health endpoint also uses configured CORS headers."""
        with patch("boto3.client") as mock_boto:
            mock_ddb = MagicMock()
            mock_ddb.describe_table.return_value = {"Table": {"TableStatus": "ACTIVE"}}
            mock_boto.return_value = mock_ddb

            result = mod._handle_system_health()

        assert result["statusCode"] == 200
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://app.example.com"

    def test_missing_allowed_origin_defaults_to_empty(self, monkeypatch):
        """Missing ALLOWED_ORIGIN defaults to empty string (doesn't crash at import)."""
        monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "http://test:8001")
        monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)

        import importlib

        from coa_ontology import api_proxy_handler

        importlib.reload(api_proxy_handler)
        assert api_proxy_handler._CORS_HEADERS["Access-Control-Allow-Origin"] == ""


class TestNamespaceIdValidation:
    """A malformed namespaceId is answered 400 here instead of being forwarded,
    where the ontology engine's graph routes surfaced it as a 500.
    """

    @pytest.mark.parametrize(
        "namespace_id",
        ["not-a-uuid", "ns-1", "", "11111111-1111-1111-8111-111111111111", "11111111-1111-4111-8111-11111111111x"],
    )
    @patch("urllib.request.urlopen")
    def test_handler_malformed_namespace_id_is_400_not_forwarded(self, mock_urlopen, mod, namespace_id):
        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/graph/datatype-property",
            "pathParameters": {"namespaceId": namespace_id},
            "queryStringParameters": {"uri": "http://example.org/o#p"},
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 400
        assert "Invalid namespaceId" in json.loads(result["body"])["message"]
        assert "Access-Control-Allow-Origin" in result["headers"]
        mock_urlopen.assert_not_called()

    @patch("coa_ontology.api_proxy_handler.idempotent_create")
    def test_handler_malformed_namespace_id_on_idempotent_post_claims_no_record(self, mock_idem, mod):
        event = {
            "httpMethod": "POST",
            "resource": "/namespaces/{namespaceId}/induce",
            "pathParameters": {"namespaceId": "garbage"},
            "queryStringParameters": None,
            "body": '{"clientToken": "t-1"}',
        }
        assert mod.handler(event, None)["statusCode"] == 400
        mock_idem.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_handler_missing_namespace_id_on_namespaced_route_is_400(self, mock_urlopen, mod):
        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/graph/class",
            "pathParameters": None,
            "queryStringParameters": None,
            "body": "",
        }
        assert mod.handler(event, None)["statusCode"] == 400
        mock_urlopen.assert_not_called()
