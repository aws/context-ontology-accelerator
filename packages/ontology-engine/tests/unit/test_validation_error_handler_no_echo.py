# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""FastAPI 422 responses must NOT echo the caller-supplied payload.

Regression: the default RequestValidationError handler on FastAPI includes
an ``input`` field containing the offending value verbatim. An XSS or SQL
fragment submitted as a value would then be reflected in the response body
— either giving a downstream front-end a rendering exposure, or giving the
attacker a probe fingerprint.

The handler installed in ``coa_ontology.main`` strips the ``input`` field
from every error entry, keeping only the shape and reason of the failure.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

pytestmark = pytest.mark.unit


def _build_app_with_handler():
    """Reproduce the ontology-engine handler wiring in a minimal app.

    The full ``coa_ontology.main`` module imports the entire service (Neptune
    clients, Bedrock, etc.), so exercise the handler via a small local app
    and assert on its externally-observable shape.
    """
    from coa_ontology.main import _validation_error_handler

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, _validation_error_handler)

    class Body(BaseModel):
        ontology_uri_prefix: str
        confidence_threshold: float

    @app.post("/echo")
    def echo(body: Body) -> dict:
        return body.model_dump()

    return app


def test_422_body_does_not_echo_caller_payload():
    """A validation error must include neither the payload key names nor their
    values in the response body.

    Uses an XSS payload as the offending value so a regression is loud:
    if the handler ever adds ``input`` back, ``<script>`` will surface in
    the response body.
    """
    client = TestClient(_build_app_with_handler())
    hostile = {
        "datasource_ids": ["ok"],
        # missing required ontology_uri_prefix; hostile extra key + value:
        "<script>alert('XSS')</script>": "https://UVCYBER.COM",
    }
    resp = client.post("/echo", json=hostile)
    assert resp.status_code == 422

    body = resp.text
    # The caller-supplied hostile key and value MUST NOT appear anywhere.
    assert "script" not in body.lower(), f"payload echoed in body: {body}"
    assert "UVCYBER" not in body, f"payload echoed in body: {body}"
    # And no "input" field either (the field-name FastAPI's default handler
    # uses to carry the echoed payload).
    payload = resp.json()
    assert "detail" in payload
    for err in payload["detail"]:
        assert "input" not in err, f"input field present in error: {err}"


def test_422_body_still_reports_field_and_reason():
    """The handler strips ``input`` but MUST preserve enough to be useful —
    ``type``, ``loc``, and ``msg`` on each error entry."""
    client = TestClient(_build_app_with_handler())
    resp = client.post("/echo", json={"confidence_threshold": "not-a-number"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert len(detail) >= 1
    for err in detail:
        # The three fields a caller legitimately needs to fix their payload
        assert "type" in err
        assert "loc" in err
        assert "msg" in err
        assert isinstance(err["loc"], list)


def test_custom_validator_msg_is_not_echoed():
    """Regression: a ``@field_validator`` that interpolates the rejected
    value into its ``ValueError`` used to leak that value through the
    ``msg`` field, even after ``input`` was stripped. The handler now
    replaces ``msg`` with a fixed string when ``type`` is ``value_error``
    / ``assertion_error``.

    This test uses an XSS payload as the offending value so a regression is
    loud: if the handler ever stops masking custom-validator ``msg``s, the
    ``<script>`` fragment surfaces in the response body.
    """
    from coa_ontology.main import _validation_error_handler
    from fastapi import FastAPI
    from pydantic import field_validator

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, _validation_error_handler)

    class Body(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def _reject_odd(cls, v: str) -> str:
            # Deliberately echoes the value — this is the pattern our fix
            # defends against, so at least one test must exercise it.
            raise ValueError(f"bad value: {v!r}")

    @app.post("/echo-name")
    def echo(body: Body) -> dict:
        return body.model_dump()

    client = TestClient(app)
    resp = client.post("/echo-name", json={"name": "<script>alert(1)</script>"})
    assert resp.status_code == 422

    body = resp.text
    assert "script" not in body.lower(), f"custom-validator msg still leaks the value: {body}"
    assert "alert" not in body.lower(), f"custom-validator msg still leaks the value: {body}"

    payload = resp.json()
    for err in payload["detail"]:
        # No ``input`` field (belt from Fix D's initial patch)
        assert "input" not in err
        # And when the error came from a custom validator (value_error /
        # assertion_error), the ``msg`` must be an opaque replacement — not
        # the raw exception text that would echo the value.
        if err["type"] in {"value_error", "assertion_error"}:
            assert err["msg"] == "Invalid value", f"custom-validator msg was not masked; got: {err['msg']!r}"
