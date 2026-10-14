# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Tier-3 OoPS! pitfall validator.

OoPSValidator posts serialized RDF to a remote OoPS! service and parses either
an XML or JSON pitfall report. We mock ``httpx.Client`` so no network call is
made and drive both response formats plus the failure paths, asserting the
``ValidationFinding`` list produced. The scan is opt-in: with no
``OOPS_ENDPOINT`` it must make no request at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from coa_ontology.validation.routers.validate import _VALIDATORS
from coa_ontology.validation.schemas import Severity, ValidationTier
from coa_ontology.validation.validators.tier3 import (
    CompetencyQuestionValidator,
    OoPSValidator,
)
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

pytestmark = pytest.mark.unit

EX = "http://example.org/"
TEST_ENDPOINT = "https://oops.example.test/rest"


@pytest.fixture(autouse=True)
def _oops_endpoint(monkeypatch):
    """Most tests exercise a configured scan; opt-out tests delete the variable."""
    monkeypatch.setenv("OOPS_ENDPOINT", TEST_ENDPOINT)


def _graph_with_one_class() -> Graph:
    g = Graph()
    g.add((URIRef(EX + "Foo"), RDF.type, OWL.Class))
    g.add((URIRef(EX + "Foo"), RDFS.label, Literal("Foo")))
    # An owl:imports triple that must be stripped before sending to OoPS.
    g.add((URIRef(EX + "ont"), OWL.imports, URIRef("http://remote/ontology")))
    return g


def _mock_httpx_client(resp: MagicMock) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.return_value = resp
    return client


class TestOoPSXmlResponse:
    def test_parses_xml_pitfalls_into_findings(self):
        xml = (
            "<OOPSResponse>"
            "<pitfalls>"
            "<P08><info><title>Missing annotations</title><importance>IMPORTANT</importance></info>"
            "<affectedElements><uri>http://example.org/Foo</uri></affectedElements></P08>"
            "</pitfalls>"
            "</OOPSResponse>"
        )
        resp = MagicMock()
        resp.text = xml
        resp.raise_for_status = MagicMock()

        with patch.object(httpx, "Client", return_value=_mock_httpx_client(resp)):
            findings = OoPSValidator().validate(_graph_with_one_class())

        assert len(findings) == 1
        f = findings[0]
        assert f.code == "OOPS_P08"
        assert "Missing annotations" in f.message
        # IMPORTANT importance maps to a warning severity.
        assert f.severity == Severity.warning
        assert f.subject == "http://example.org/Foo"

    def test_minor_importance_maps_to_info(self):
        xml = (
            "<OOPSResponse><pitfalls>"
            "<P04><info><title>Unconnected ontology element</title><importance>MINOR</importance></info></P04>"
            "</pitfalls></OOPSResponse>"
        )
        resp = MagicMock()
        resp.text = xml
        resp.raise_for_status = MagicMock()
        with patch.object(httpx, "Client", return_value=_mock_httpx_client(resp)):
            findings = OoPSValidator().validate(_graph_with_one_class())
        assert findings[0].severity == Severity.info
        assert findings[0].subject is None  # no <uri> elements


class TestOoPSJsonResponse:
    def test_parses_json_pitfalls_into_findings(self):
        resp = MagicMock()
        resp.text = "  {not-xml"  # does NOT start with '<' → JSON branch
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "pitfalls": {
                "P11": [
                    {
                        "info": {"title": "Missing domain or range", "importance": "CRITICAL"},
                        "resources": [{"uri": "http://example.org/A"}, {"uri": "http://example.org/B"}],
                    }
                ]
            }
        }
        with patch.object(httpx, "Client", return_value=_mock_httpx_client(resp)):
            findings = OoPSValidator().validate(_graph_with_one_class())
        assert len(findings) == 1
        f = findings[0]
        assert f.code == "OOPS_P11"
        assert f.severity == Severity.warning  # CRITICAL → warning
        assert f.subject == "http://example.org/A"
        assert "<http://example.org/A>" in f.message

    def test_json_truncates_affected_resource_list(self):
        resp = MagicMock()
        resp.text = "{}"
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "pitfalls": {
                "P10": [
                    {
                        "info": {"title": "Missing disjointness", "importance": "MINOR"},
                        "resources": [{"uri": f"http://example.org/R{i}"} for i in range(8)],
                    }
                ]
            }
        }
        with patch.object(httpx, "Client", return_value=_mock_httpx_client(resp)):
            findings = OoPSValidator().validate(_graph_with_one_class())
        # 8 resources → 5 shown + "(+3 more)".
        assert "(+3 more)" in findings[0].message
        assert findings[0].severity == Severity.info


class TestOoPSFailurePaths:
    def test_http_error_produces_unavailable_finding(self):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.side_effect = httpx.ConnectError("connection refused")
        with patch.object(httpx, "Client", return_value=client):
            findings = OoPSValidator().validate(_graph_with_one_class())
        assert len(findings) == 1
        assert findings[0].code == "OOPS_UNAVAILABLE"
        assert findings[0].severity == Severity.info

    def test_unexpected_error_produces_error_finding(self):
        resp = MagicMock()
        resp.text = "{}"
        resp.raise_for_status = MagicMock()
        # json() raising a non-HTTP error hits the generic Exception branch.
        resp.json.side_effect = ValueError("bad decode")
        with patch.object(httpx, "Client", return_value=_mock_httpx_client(resp)):
            findings = OoPSValidator().validate(_graph_with_one_class())
        assert len(findings) == 1
        assert findings[0].code == "OOPS_ERROR"
        assert "bad decode" in findings[0].message


class TestCompetencyQuestionValidator:
    def test_no_questions_returns_empty(self):
        assert CompetencyQuestionValidator().validate(Graph()) == []

    def test_questions_flagged_as_not_evaluated(self):
        findings = CompetencyQuestionValidator().validate(
            Graph(), competency_questions=["What agreements exist?", "Who are the parties?"]
        )
        assert len(findings) == 2
        assert all(f.code == "CQ_NOT_EVALUATED" for f in findings)
        assert findings[0].details["question"] == "What agreements exist?"


class TestOoPSOptIn:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_no_endpoint_makes_no_request(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("OOPS_ENDPOINT", raising=False)
        else:
            monkeypatch.setenv("OOPS_ENDPOINT", value)
        with patch.object(httpx, "Client") as client_cls:
            findings = OoPSValidator().validate(_graph_with_one_class())
        client_cls.assert_not_called()
        assert [f.code for f in findings] == ["OOPS_NOT_CONFIGURED"]
        assert findings[0].severity == Severity.info
        assert "OOPS_ENDPOINT" in findings[0].message

    def test_configured_endpoint_is_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv("OOPS_ENDPOINT", "https://self-hosted.example.test/oops/rest")
        resp = MagicMock()
        resp.text = "<OOPSResponse></OOPSResponse>"
        resp.raise_for_status = MagicMock()
        client = _mock_httpx_client(resp)
        with patch.object(httpx, "Client", return_value=client):
            findings = OoPSValidator().validate(_graph_with_one_class())
        assert client.post.call_args.args[0] == "https://self-hosted.example.test/oops/rest"
        assert findings == []

    def test_validate_router_default_tiers_send_nothing_without_endpoint(self, monkeypatch):
        """The standalone validate router's Tier-3 list (shared with proposal validation) stays local."""
        monkeypatch.delenv("OOPS_ENDPOINT", raising=False)
        tier3 = _VALIDATORS[ValidationTier.tier3_review]
        assert any(isinstance(v, OoPSValidator) for v in tier3)
        findings = []
        with patch.object(httpx, "Client") as client_cls:
            for v in tier3:
                findings.extend(v.validate(_graph_with_one_class(), competency_questions=["Q?"]))
        client_cls.assert_not_called()
        codes = [f.code for f in findings]
        assert codes.count("OOPS_NOT_CONFIGURED") == 1
        # The other Tier-3 validators still ran.
        assert any(f.validator != "oops" for f in findings)
