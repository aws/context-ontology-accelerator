# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the RDF_SPARQL source configuration contract."""

from __future__ import annotations

import json

import pytest
from coa_common.sparql_config import (
    GRAPH_SOURCES_DISABLED_MESSAGE,
    GRAPH_SOURCES_ENV,
    SparqlAuthMode,
    SparqlConfigurationError,
    SparqlStore,
    graph_sources_enabled,
    parse_sparql_configuration,
)

pytestmark = pytest.mark.unit

_HOST = "db.cluster-abc.us-east-1.neptune.amazonaws.com"


def test_parse_minimal_neptune_config_applies_store_defaults():
    cfg = parse_sparql_configuration({"dataEndpoint": _HOST})
    assert cfg.store is SparqlStore.NEPTUNE
    assert cfg.auth_mode is SparqlAuthMode.SIGV4
    assert cfg.effective_port == 8182
    assert cfg.base_url == f"https://{_HOST}:8182"
    assert cfg.query_url == f"https://{_HOST}:8182/sparql"


def test_parse_accepts_the_stored_json_string():
    cfg = parse_sparql_configuration(json.dumps({"store": "neptune", "dataEndpoint": _HOST, "port": 8182}))
    assert cfg.query_url == f"https://{_HOST}:8182/sparql"


def test_endpoint_url_with_port_and_path_is_used_verbatim():
    cfg = parse_sparql_configuration({"dataEndpoint": f"https://{_HOST}:8182/custom/sparql"})
    assert cfg.effective_port == 8182
    assert cfg.query_url == f"https://{_HOST}:8182/custom/sparql"


def test_endpoint_port_conflicting_with_port_field_is_rejected():
    with pytest.raises(SparqlConfigurationError, match="different ports"):
        parse_sparql_configuration({"dataEndpoint": f"{_HOST}:8183", "port": 8182})


def test_neptune_rejects_a_credential_secret():
    with pytest.raises(SparqlConfigurationError, match="credentialSecretArn is not accepted"):
        parse_sparql_configuration(
            {
                "dataEndpoint": _HOST,
                "credentialSecretArn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:x",
            }
        )


def test_unknown_store_is_rejected():
    with pytest.raises(SparqlConfigurationError, match="store"):
        parse_sparql_configuration({"store": "stardog", "dataEndpoint": _HOST})


def test_unknown_auth_mode_is_rejected():
    with pytest.raises(SparqlConfigurationError, match="authMode"):
        parse_sparql_configuration({"dataEndpoint": _HOST, "authMode": "token"})


@pytest.mark.parametrize(
    "graph",
    ["https://example.com/a>b", "https://example.com/a b", "urn:x", 'https://example.com/"x'],
)
def test_named_graph_that_could_break_out_of_an_iriref_is_rejected(graph):
    with pytest.raises(SparqlConfigurationError, match="dataNamedGraph"):
        parse_sparql_configuration({"dataEndpoint": _HOST, "dataNamedGraph": graph})


@pytest.mark.parametrize("endpoint", ["http://db.example.com", "db.example.com;x=1", "https://u@db.example.com"])
def test_malformed_endpoint_is_rejected(endpoint):
    with pytest.raises(SparqlConfigurationError, match="dataEndpoint"):
        parse_sparql_configuration({"dataEndpoint": endpoint})


def test_missing_endpoint_is_rejected():
    with pytest.raises(SparqlConfigurationError, match="dataEndpoint"):
        parse_sparql_configuration({"store": "neptune"})


@pytest.mark.parametrize("raw", [None, "", "not json", "[1]"])
def test_unparseable_configuration_is_rejected(raw):
    with pytest.raises(SparqlConfigurationError):
        parse_sparql_configuration(raw)


def test_signing_region_prefers_the_configured_region():
    eu = "db.cluster-abc.eu-west-1.neptune.amazonaws.com"
    assert parse_sparql_configuration({"dataEndpoint": eu, "region": "eu-west-1"}).signing_region("us-east-1") == (
        "eu-west-1"
    )
    assert parse_sparql_configuration({"dataEndpoint": _HOST}).signing_region("us-east-1") == "us-east-1"


def test_to_record_round_trips_camel_case_with_defaults_filled():
    cfg = parse_sparql_configuration(
        {"dataEndpoint": _HOST, "region": "us-east-1", "dataNamedGraph": "https://example.test/g"}
    )
    record = cfg.to_record()
    assert record == {
        "store": "neptune",
        "dataEndpoint": _HOST,
        "region": "us-east-1",
        "dataNamedGraph": "https://example.test/g",
        "authMode": "sigv4",
    }
    assert parse_sparql_configuration(json.dumps(record)) == cfg


def test_unknown_keys_in_a_stored_blob_are_ignored():
    assert parse_sparql_configuration({"dataEndpoint": _HOST, "addedLater": 1}).host == _HOST


class TestNeptuneConfinement:
    """A Neptune source names a cluster endpoint in its signing region, on 8182, and nothing else."""

    @pytest.mark.parametrize(
        "host",
        [
            "db.cluster-abc123.us-east-1.neptune.amazonaws.com",
            "db.cluster-ro-abc123.us-east-1.neptune.amazonaws.com",
            "reports.cluster-custom-abc123.us-east-1.neptune.amazonaws.com",
            "https://db.cluster-abc123.us-east-1.neptune.amazonaws.com:8182/sparql",
        ],
    )
    def test_a_cluster_endpoint_in_the_region_on_8182_is_accepted(self, host):
        assert parse_sparql_configuration({"dataEndpoint": host, "region": "us-east-1"}).effective_port == 8182

    @pytest.mark.parametrize(
        "host",
        [
            "example.com",
            "metadata.internal",
            "10.0.0.5",
            "169.254.169.254",
            "db.cluster-abc123.us-east-1.neptune.amazonaws.com.evil.example",
            "db.abc123.us-east-1.neptune.amazonaws.com",
            "db.cluster-abc123.us-east-1.rds.amazonaws.com",
        ],
    )
    def test_any_other_host_is_refused(self, host):
        with pytest.raises(SparqlConfigurationError, match="Neptune cluster endpoint"):
            parse_sparql_configuration({"dataEndpoint": host, "region": "us-east-1"})

    @pytest.mark.parametrize("config", [{"port": 443}, {"port": 9000}, {"dataEndpoint": f"{_HOST}:9000"}])
    def test_another_port_is_refused(self, config):
        with pytest.raises(SparqlConfigurationError, match="8182"):
            parse_sparql_configuration({"dataEndpoint": _HOST, "region": "us-east-1", **config})

    def test_a_host_in_another_region_than_the_signing_region_is_refused(self):
        with pytest.raises(SparqlConfigurationError, match="region"):
            parse_sparql_configuration({"dataEndpoint": _HOST, "region": "us-west-2"})


@pytest.mark.parametrize(
    ("member", "value"),
    [
        ("dataNamedGraph", "https://example.test/g\n"),
        ("region", "us-east-1\n"),
        ("dataEndpoint", _HOST + "\n"),
    ],
)
def test_a_value_with_a_trailing_newline_is_refused(member, value):
    """``^...$`` with ``match`` lets one trailing newline through; each value must be the whole match."""
    config = {"dataEndpoint": _HOST, member: value}
    with pytest.raises(SparqlConfigurationError, match=member):
        parse_sparql_configuration(config)


class TestGraphSourcesSwitch:
    """One deploy-time switch, off unless the deployment turns it on."""

    def test_off_when_unset(self, monkeypatch):
        monkeypatch.delenv(GRAPH_SOURCES_ENV, raising=False)
        assert graph_sources_enabled() is False

    @pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "anything"])
    def test_off_unless_explicitly_on(self, monkeypatch, value):
        monkeypatch.setenv(GRAPH_SOURCES_ENV, value)
        assert graph_sources_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", " on ", "yes"])
    def test_on_for_the_truthy_spellings(self, monkeypatch, value):
        monkeypatch.setenv(GRAPH_SOURCES_ENV, value)
        assert graph_sources_enabled() is True

    def test_the_variable_and_the_message_name_the_deploy_switch(self):
        assert GRAPH_SOURCES_ENV == "GRAPH_SOURCES_ENABLED"
        assert "graph_sources" in GRAPH_SOURCES_DISABLED_MESSAGE
