# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Suite-wide setup and the shared fakes: the caller, its token provider, and a client on them."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from coa_control_plane_server.models.create_source_input import CreateSourceInput
from coa_control_plane_server.models.start_induction_request_content import StartInductionRequestContent
from coa_workflow_agents.auth.caller import Caller
from coa_workflow_agents.clients.coa_api_client import CoaApiClient, CoaApiError, _wire_body

# LOG_LEVEL is a common shell variable. server.py reads it at import (an
# invalid value fails there), and the config tests assert the default, so a
# developer's own value must not leak into the suite. Tests that need a level
# set it with monkeypatch.
for _name in ("LOG_LEVEL",):
    os.environ.pop(_name, None)

# A token expiry no test outlives (year 2100).
FAR_EXPIRY = 4102444800.0
CALLER_TOKEN = "caller.jwt"
BASE_URL = "https://coa.example.com/prod/"


class FakeTokenProvider:
    """Returns a fixed token and counts how often it was asked."""

    def __init__(self, token: str = CALLER_TOKEN) -> None:
        self.token = token
        self.calls = 0

    def get_id_token(self) -> str:
        self.calls += 1
        return self.token


@pytest.fixture
def make_caller() -> Callable[..., Caller]:
    """Build a verified caller; ``expires_at`` defaults to far in the future."""

    def build(*, expires_at: float = FAR_EXPIRY) -> Caller:
        return Caller(email="a@example.com", groups=("g1",), id_token=CALLER_TOKEN, expires_at=expires_at)

    return build


@pytest.fixture
def caller(make_caller: Callable[..., Caller]) -> Caller:
    """A caller whose token outlives every test."""
    return make_caller()


@pytest.fixture
def token_provider() -> FakeTokenProvider:
    return FakeTokenProvider()


@pytest.fixture
def coa_client(token_provider: FakeTokenProvider) -> Iterator[CoaApiClient]:
    """A client on ``token_provider``; patch its ``_session`` to fake the HTTP layer."""
    with CoaApiClient(base_url=BASE_URL, token_provider=token_provider) as client:
        yield client


class FakeCoaClient:
    """Scripted COA endpoints for the tool tests: records what the tools send, makes no request.

    Induction: ``jobs`` (the polled job, the last repeating), ``started``, ``start_error``.
    Sources: ``sources`` (each ``get_source`` read, the last repeating; an exception is raised),
    ``created``, ``create_error`` and ``create_headers`` for ``create_source``, and ``errors`` (a method
    name to the exception that call raises) for any other source call; ``create_source`` uses
    ``create_error``. ``pages`` scripts every paged list. ``calls`` records each source call as
    ``(name, args, kwargs)``.
    """

    def __init__(
        self,
        jobs: list[dict[str, Any]] | None = None,
        *,
        started: dict[str, Any] | None = None,
        start_error: CoaApiError | None = None,
        pages: list[Any] | None = None,
        sources: list[Any] | None = None,
        created: dict[str, Any] | list[dict[str, Any]] | None = None,
        create_error: Exception | list[Exception | None] | None = None,
        create_headers: dict[str, str] | list[dict[str, str]] | None = None,
        scan_job: dict[str, Any] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.jobs = jobs or [{"jobId": "j1", "status": "completed"}]
        self.started = {"job_id": "j1"} if started is None else started
        self.start_error = start_error
        self.pages = pages or [{"items": []}]
        self.sources = sources or [{"sourceId": "src1", "status": "APPROVED"}]
        self.created = {"sourceId": "src1", "status": "REGISTERED"} if created is None else created
        self.create_calls = 0
        self.create_error = create_error
        self.create_headers = create_headers or {}
        self.scan_job = scan_job or {}
        self.errors = errors or {}
        self.bodies: list[dict[str, Any]] = []
        self.tokens: list[str | None] = []
        self.polled: list[tuple[str, str | None]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if name in self.errors:
            raise self.errors[name]

    def names(self) -> list[str]:
        """The source calls made, in order."""
        return [name for name, _, _ in self.calls]

    # --- induction ---

    def start_induction(self, namespace_id, request, *, client_token=None):
        assert isinstance(request, StartInductionRequestContent)
        self.bodies.append(_wire_body(request))
        self.tokens.append(client_token)
        if self.start_error:
            raise self.start_error
        return self.started

    def get_induction_job(self, namespace_id, job_id, *, include=None):
        self.polled.append((job_id, include))
        return self.jobs.pop(0) if len(self.jobs) > 1 else self.jobs[0]

    def _page(self, **kwargs):
        self.list_calls.append(kwargs)
        if len(self.list_calls) > 5:
            raise AssertionError("pagination did not terminate")
        return self.pages[min(len(self.list_calls), len(self.pages)) - 1]

    def list_induction_jobs(self, namespace_id, *, status=None, next_token=None, max_results=50):
        return self._page(status=status, next_token=next_token, max_results=max_results)

    def list_induced_datasources(self, namespace_id, *, next_token=None, max_results=1000):
        return self._page(next_token=next_token, max_results=max_results)

    # --- sources ---

    def get_source(self, namespace_id, source_id):
        self._record("get_source", namespace_id, source_id)
        response = self.sources.pop(0) if len(self.sources) > 1 else self.sources[0]
        if isinstance(response, Exception):
            raise response
        return response

    def create_source(self, namespace_id, request, *, client_token=None, headers_out=None):
        assert isinstance(request, CreateSourceInput)
        self._record("create_source", namespace_id, client_token=client_token)
        self.bodies.append(_wire_body(request))
        self.tokens.append(client_token)
        n = self.create_calls
        self.create_calls += 1

        def nth(value: Any) -> Any:
            return value[min(n, len(value) - 1)] if isinstance(value, list) and value else value

        if headers_out is not None:
            headers_out.update(nth(self.create_headers))
        error = nth(self.create_error)
        if error is not None:
            raise error
        return dict(nth(self.created))

    def list_sources(self, namespace_id, *, source_type=None, next_token=None, max_results=50):
        return self._page(source_type=source_type, next_token=next_token, max_results=max_results)

    def get_source_scan_job(self, namespace_id, source_id, job_id):
        self._record("get_source_scan_job", namespace_id, source_id, job_id)
        return self.scan_job

    def list_source_scan_jobs(self, namespace_id, source_id, *, next_token=None, max_results=1000):
        return self._page(source_id=source_id, next_token=next_token, max_results=max_results)

    def approve_source(self, namespace_id, source_id):
        self._record("approve_source", namespace_id, source_id)
        return {}

    def reject_source(self, namespace_id, source_id):
        self._record("reject_source", namespace_id, source_id)
        return {}

    def rescan_source(self, namespace_id, source_id, *, confirm_discard_open_review=None, client_token=None):
        self._record(
            "rescan_source",
            namespace_id,
            source_id,
            confirm_discard_open_review=confirm_discard_open_review,
            client_token=client_token,
        )
        return {}

    def delete_source(self, namespace_id, source_id):
        self._record("delete_source", namespace_id, source_id)
        return {}

    def update_source_metadata(self, namespace_id, source_id, request):
        self._record("update_source_metadata", namespace_id, source_id)
        self.bodies.append(_wire_body(request))
        return {"sourceId": source_id}

    def put_source_rescan_schedule(self, namespace_id, source_id, request):
        self._record("put_source_rescan_schedule", namespace_id, source_id)
        self.bodies.append(_wire_body(request))
        return {"sourceId": source_id}

    def put_source_event_rescan(self, namespace_id, source_id, *, enabled):
        self._record("put_source_event_rescan", namespace_id, source_id, enabled=enabled)
        return {"sourceId": source_id}

    def keep_rescan_removal(self, namespace_id, source_id, table_id, *, column_name=None):
        self._record("keep_rescan_removal", namespace_id, source_id, table_id, column_name=column_name)
        return {"tableId": table_id}

    def get_source_upload_urls(self, namespace_id, files):
        self._record("get_source_upload_urls", namespace_id, files=files)
        return {
            "uploadId": "11111111-2222-4333-8444-555555555555",
            "uploadUrls": [
                {"filename": f["filename"], "uploadUrl": f"https://s3.example/{f['filename']}"} for f in files
            ],
        }

    def put_presigned(self, url, data, content_type):
        self._record("put_presigned", url, len(data), content_type)
