# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The ``configuration`` contract of an ``RDF_SPARQL`` source.

One model read by every party that touches a graph source: the sources API validates
and persists it, the graph-verify Lambda reads it to pick and run its reachability
probe, and serve reads it to build the SPARQL client. Keeping it here — rather than a
parse per consumer — is what makes ``store`` the single engine discriminator: a new
SPARQL engine is a new :class:`SparqlStore` member plus its :class:`SparqlStoreSpec`,
then one client arm (serve) and one probe arm (verify), and never a new source type.

The Smithy ``SparqlConfiguration`` shape is the wire form of the same contract; the
cross-field rules (which auth modes a store accepts, whether it may carry a credential
secret) cannot be expressed in Smithy and are enforced here.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# The deploy-time switch for graph sources (CDK context ``graph_sources``). Infra sets the
# variable only when the switch is on, so an unset variable means off.
GRAPH_SOURCES_ENV = "GRAPH_SOURCES_ENABLED"
GRAPH_SOURCES_DISABLED_MESSAGE = (
    "Graph sources are turned off in this deployment. To register or verify an RDF_SPARQL source, "
    "redeploy with -c graph_sources=on (SCL_GRAPH_SOURCES=on make deploy-dev)."
)


def graph_sources_enabled() -> bool:
    """Whether this deployment serves graph (RDF_SPARQL) sources: off unless 1/true/on/yes."""
    return os.environ.get(GRAPH_SOURCES_ENV, "").strip().lower() in ("1", "true", "on", "yes")


class SparqlStore(StrEnum):
    """SPARQL engine behind an ``RDF_SPARQL`` source (Smithy ``SparqlStore``)."""

    NEPTUNE = "neptune"


class SparqlAuthMode(StrEnum):
    """How the client authenticates to the endpoint (Smithy ``SparqlAuthMode``)."""

    SIGV4 = "sigv4"


@dataclass(frozen=True)
class SparqlStoreSpec:
    """What a store accepts and how it is addressed."""

    auth_modes: frozenset[SparqlAuthMode]
    default_port: int
    # Path of the SPARQL query endpoint when ``dataEndpoint`` carries none.
    default_query_path: str
    accepts_credential_secret: bool
    # Where the store may be addressed: the endpoint hosts it accepts and the
    # ports they listen on, so a registration cannot point the verify probe anywhere else.
    host_re: re.Pattern[str]
    allowed_ports: frozenset[int]


STORE_SPECS: dict[SparqlStore, SparqlStoreSpec] = {
    SparqlStore.NEPTUNE: SparqlStoreSpec(
        auth_modes=frozenset({SparqlAuthMode.SIGV4}),
        default_port=8182,
        default_query_path="/sparql",
        # SigV4 with the caller's IAM role is the identity; there is no secret to hold.
        accepts_credential_secret=False,
        # A cluster, reader or custom cluster endpoint; never an instance endpoint, an IP
        # literal or another domain.
        host_re=re.compile(
            r"^[a-z][a-z0-9-]{0,62}\.cluster-(ro-|custom-)?[a-z0-9]+"
            r"\.(?P<region>[a-z]{2}(-[a-z]+)+-[0-9])\.neptune\.amazonaws\.com$"
        ),
        allowed_ports=frozenset({8182}),
    ),
}

DEFAULT_SPARQL_STORE = SparqlStore.NEPTUNE

# Mirrors of the Smithy patterns, kept in sync by tests/unit/test_smithy_python_pattern_sync.py.
# Re-checked here because a blob read back from DynamoDB never passed through the
# generated request model, and both values end up in a request: the endpoint in the
# URL, the named graph inside ``GRAPH <...>``.
SPARQL_ENDPOINT_RE = re.compile(
    r"^(https://)?(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*)(:(?P<port>[0-9]{1,5}))?(?P<path>/[A-Za-z0-9._~/-]*)?$"
)
SPARQL_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
NAMED_GRAPH_IRI_RE = re.compile(r"^https?://[^\s<>\"{}|\\^`]+$")
_SECRET_ARN_RE = re.compile(r"^arn:(aws|aws-us-gov):secretsmanager:[a-z0-9-]+:\d{12}:secret:.+$")


class SparqlConfigurationError(ValueError):
    """A graph source ``configuration`` that violates the contract."""


@dataclass(frozen=True)
class _EndpointParts:
    host: str
    port: int | None
    path: str | None


def _split_endpoint(value: str) -> _EndpointParts:
    match = SPARQL_ENDPOINT_RE.fullmatch(value)
    if not match:
        raise ValueError("dataEndpoint must be a host or an https:// URL with optional port and path")
    port = match.group("port")
    if port is not None and not 1 <= int(port) <= 65535:
        raise ValueError("dataEndpoint port must be between 1 and 65535")
    return _EndpointParts(host=match.group("host"), port=int(port) if port else None, path=match.group("path"))


class SparqlSourceConfig(BaseModel):
    """Parsed, validated ``configuration`` of an ``RDF_SPARQL`` source."""

    # Stored blobs are camelCase (the Smithy member names); unknown keys are ignored so a
    # field added later does not make every older reader reject the record.
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore")

    store: SparqlStore = DEFAULT_SPARQL_STORE
    data_endpoint: str = Field(alias="dataEndpoint")
    region: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    data_named_graph: str | None = Field(default=None, alias="dataNamedGraph")
    auth_mode: SparqlAuthMode = Field(default=SparqlAuthMode.SIGV4, alias="authMode")
    credential_secret_arn: str | None = Field(default=None, alias="credentialSecretArn")

    @field_validator("data_endpoint")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        _split_endpoint(value)
        return value

    @field_validator("region")
    @classmethod
    def _check_region(cls, value: str | None) -> str | None:
        if value is not None and not SPARQL_REGION_RE.fullmatch(value):
            raise ValueError("region must be an AWS region code, e.g. us-east-1")
        return value

    @field_validator("data_named_graph")
    @classmethod
    def _check_named_graph(cls, value: str | None) -> str | None:
        if value is not None and not NAMED_GRAPH_IRI_RE.fullmatch(value):
            raise ValueError(
                "dataNamedGraph must be an absolute http(s) IRI with no characters illegal in a SPARQL IRI"
            )
        return value

    @field_validator("credential_secret_arn")
    @classmethod
    def _check_secret_arn(cls, value: str | None) -> str | None:
        if value is not None and not _SECRET_ARN_RE.match(value):
            raise ValueError("credentialSecretArn must be a Secrets Manager secret ARN")
        return value

    @model_validator(mode="after")
    def _check_store_rules(self) -> SparqlSourceConfig:
        spec = self.spec
        if self.auth_mode not in spec.auth_modes:
            allowed = ", ".join(sorted(m.value for m in spec.auth_modes))
            raise ValueError(
                f"authMode {self.auth_mode.value!r} is not supported for store {self.store.value!r} "
                f"(allowed: {allowed})"
            )
        if self.credential_secret_arn and not spec.accepts_credential_secret:
            raise ValueError(f"credentialSecretArn is not accepted for store {self.store.value!r}")
        endpoint_port = _split_endpoint(self.data_endpoint).port
        if endpoint_port is not None and self.port is not None and endpoint_port != self.port:
            raise ValueError("dataEndpoint and port name different ports")
        host = spec.host_re.match(self.host)
        if host is None:
            raise ValueError(
                f"dataEndpoint must be a {self.store.value.capitalize()} cluster endpoint "
                "(<name>.cluster[-ro|-custom]-<id>.<region>.neptune.amazonaws.com)"
            )
        if self.region is not None and host.group("region") != self.region:
            raise ValueError(
                f"dataEndpoint is in region {host.group('region')}, but the source signs for {self.region}"
            )
        if self.effective_port not in spec.allowed_ports:
            allowed = ", ".join(str(p) for p in sorted(spec.allowed_ports))
            raise ValueError(f"port must be {allowed} for store {self.store.value!r}")
        return self

    @property
    def spec(self) -> SparqlStoreSpec:
        """Capabilities and addressing defaults of this source's store."""
        return STORE_SPECS[self.store]

    @property
    def host(self) -> str:
        """Endpoint host, without scheme, port or path."""
        return _split_endpoint(self.data_endpoint).host

    @property
    def effective_port(self) -> int:
        """Port from ``dataEndpoint``, else ``port``, else the store default."""
        endpoint_port = _split_endpoint(self.data_endpoint).port
        if endpoint_port is not None:
            return endpoint_port
        return self.port if self.port is not None else self.spec.default_port

    @property
    def base_url(self) -> str:
        """``https://host:port`` — the root a management/status probe addresses."""
        return f"https://{self.host}:{self.effective_port}"

    @property
    def query_url(self) -> str:
        """The SPARQL query endpoint the A-Box is read through."""
        path = _split_endpoint(self.data_endpoint).path
        if not path or path == "/":
            path = self.spec.default_query_path
        return f"{self.base_url}{path}"

    def signing_region(self, default: str) -> str:
        """Region to sign SigV4 requests for: the configured one, else ``default``."""
        return self.region or default

    def to_record(self) -> dict[str, Any]:
        """The camelCase blob persisted as the source's ``configuration``."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


def parse_sparql_configuration(raw: str | dict[str, Any] | None) -> SparqlSourceConfig:
    """Parse a stored or requested ``configuration`` into a validated model.

    Accepts the JSON string the sources table stores, or an already-decoded dict
    (some older rows store a map). Raises :class:`SparqlConfigurationError` with a
    caller-presentable message on any violation.
    """
    if raw is None or raw == "":
        raise SparqlConfigurationError("configuration is missing")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SparqlConfigurationError("configuration is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise SparqlConfigurationError("configuration must be a JSON object")
    try:
        return SparqlSourceConfig.model_validate(raw)
    except ValidationError as exc:
        messages = "; ".join(_error_message(err) for err in exc.errors())
        raise SparqlConfigurationError(messages) from exc


def _error_message(err: Any) -> str:
    loc = ".".join(str(part) for part in err.get("loc", ()) if part != "__root__")
    msg = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
    return f"{loc}: {msg}" if loc else msg
