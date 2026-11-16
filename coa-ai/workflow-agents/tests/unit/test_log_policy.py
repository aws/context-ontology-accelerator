# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the fail-closed log content policy (``log_policy.py``)."""

from __future__ import annotations

import ast
import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import coa_workflow_agents
import pytest
import structlog
from coa_workflow_agents import observability
from coa_workflow_agents.log_policy import (
    _MAX_LIST_ITEMS,
    EVENT_FIELDS,
    REDACTED,
    WITHHELD,
    redact,
    setup_service_logging,
)

pytestmark = pytest.mark.unit

SENTINEL = "SENTINEL-caller-data-7f3a"
_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})
# Declared events logged by libraries running in this process (coa_common's
# inbound token authorizer), not by this package.
_LIBRARY_EVENTS = frozenset(
    {
        "JWT validation failed",
        "Failed to fetch JWKS",
        "Failed to fetch OIDC discovery document",
        "JWKS fetch failed recently; not retrying yet",
    }
)
_SRC = Path(coa_workflow_agents.__file__).parent


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    """The production logging setup, writing to an in-memory stream."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    out = io.StringIO()
    setup_service_logging("INFO")
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=out), cache_logger_on_first_use=False)
    for handler in root.handlers:
        assert isinstance(handler, logging.StreamHandler)
        handler.setStream(out)
    try:
        yield out
    finally:
        structlog.reset_defaults()
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


def _lines(out: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_redact_known_event_keeps_declared_fields_and_bookkeeping():
    event = {
        "event": "poll_timeout",
        "level": "warning",
        "run_id": "abc",
        "operation": "ontology_delete",
        "timeout_s": 1.5,
    }
    assert redact(None, "warning", dict(event)) == event


def test_redact_new_unknown_field_on_existing_event_is_dropped():
    out = redact(None, "info", {"event": "run_workflow_completed", "outcome": "success", "note": SENTINEL})
    assert out == {"event": "run_workflow_completed", "outcome": "success"}


def test_redact_declared_field_with_unsafe_value_is_redacted():
    out = redact(
        None,
        "info",
        {"event": "tool_call_completed", "tool": f"two words {SENTINEL}", "agent": {"x": 1}, "duration_s": 0.5},
    )
    assert out == {"event": "tool_call_completed", "tool": REDACTED, "agent": REDACTED, "duration_s": 0.5}


def test_redact_unknown_event_keeps_bookkeeping_only():
    out = redact(None, "info", {"event": "new_event", "level": "info", "detail": SENTINEL})
    assert out == {"event": "new_event", "level": "info"}


def test_redact_event_name_that_is_not_an_identifier_is_redacted():
    assert redact(None, "info", {"event": f"user said {SENTINEL}"}) == {"event": REDACTED}


def test_redact_declared_library_event_keeps_its_name_and_drops_its_fields():
    out = redact(None, "warning", {"event": "JWT validation failed", "level": "warning", "error": SENTINEL})
    assert out == {"event": "JWT validation failed", "level": "warning"}


def test_redact_exception_text_is_dropped():
    out = redact(None, "error", {"event": "run_workflow_failed", "error": "RuntimeError", "exception": SENTINEL})
    assert out == {"event": "run_workflow_failed", "error": "RuntimeError"}


def test_redact_lists_keep_safe_items_only():
    out = redact(None, "info", {"event": "coa_workflow_agents_server_starting", "mutating_tools": ["a_b", "c d"]})
    assert out == {"event": "coa_workflow_agents_server_starting", "mutating_tools": ["a_b", REDACTED]}


def test_redact_list_over_the_item_cap_is_redacted():
    items = ["a_b"] * (_MAX_LIST_ITEMS + 1)
    out = redact(None, "info", {"event": "coa_workflow_agents_server_starting", "mutating_tools": items})
    assert out == {"event": "coa_workflow_agents_server_starting", "mutating_tools": REDACTED}


def test_structlog_event_rendered_unknown_field_never_reaches_the_line(stream: io.StringIO):
    structlog.get_logger("t").info("tool_call_completed", tool="get_source", outcome="success", extra=SENTINEL)
    (line,) = _lines(stream)
    assert line["tool"] == "get_source" and "extra" not in line
    assert SENTINEL not in stream.getvalue()


def test_foreign_record_args_and_traceback_never_reach_the_line(stream: io.StringIO):
    try:
        raise RuntimeError(f"boom {SENTINEL}")
    except RuntimeError:
        logging.getLogger("strands.agent._agent_as_tool").warning(
            "tool_name=<%s> | agent invocation failed: %s", SENTINEL, SENTINEL, exc_info=True
        )
    (line,) = _lines(stream)
    assert line["event"] == "tool_name=<%s> | agent invocation failed: %s"
    assert line["logger"] == "strands.agent._agent_as_tool"
    assert line["level"] == "warning"
    assert line.get("exc_type") == "RuntimeError"
    assert "exception" not in line
    assert SENTINEL not in stream.getvalue()


def test_foreign_record_without_args_is_withheld(stream: io.StringIO):
    # An f-string the library formatted itself: indistinguishable from a constant.
    logging.getLogger("mcp.server.lowlevel.server").error(f"Received exception from stream: {SENTINEL}")
    (line,) = _lines(stream)
    assert line["event"] == WITHHELD
    assert SENTINEL not in stream.getvalue()


def _logged_events() -> dict[str, set[str]]:
    """Every ``logger.<level>("<event>", field=...)`` call in the package: event -> its field names."""
    events: dict[str, set[str]] = {}
    for path in _SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in _LOG_METHODS or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                fields = {kw.arg for kw in node.keywords if kw.arg is not None}
                events.setdefault(first.value, set()).update(fields)
    # ``logger.info("run_token_usage", **totals, ...)``: the splat's keys are the usage fields.
    events["run_token_usage"].update(name for _, name in observability._USAGE_FIELDS)
    return events


def test_event_fields_declare_every_event_and_field_the_package_logs():
    logged = _logged_events()
    assert set(logged) == set(EVENT_FIELDS) - _LIBRARY_EVENTS
    for event, fields in logged.items():
        assert fields <= EVENT_FIELDS[event], event


@pytest.mark.parametrize(
    ("event", "field", "value", "kept"),
    [
        ("run_workflow_failed", "error", "RuntimeError", True),
        ("run_workflow_failed", "error", "CoaApiError: GET /namespaces/{id}/metrics/{id} -> HTTP 404", True),
        ("run_workflow_failed", "error", f"RuntimeError: model failed on {SENTINEL}", False),
        ("coa_api_client.idempotent_post_retry", "reason", "HTTP 409 in progress", True),
        ("coa_api_client.idempotent_post_retry", "reason", "ConnectionError", True),
        ("coa_api_client.idempotent_post_retry", "reason", f"HTTP 503 {SENTINEL}", False),
    ],
)
def test_redact_code_built_fields_keep_only_their_exact_shape(event, field, value, kept):
    assert redact(None, "info", {"event": event, field: value})[field] == (value if kept else REDACTED)
