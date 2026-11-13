# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed log content policy: only declared, identifier-shaped fields reach a log line.

Caller data (instructions, tool arguments and results, model text, names and IRIs,
response bodies, exception messages) is never logged. Careful log calls are not enough:
libraries log what they like. So every line passes ``redact`` last:

- This package's structlog events keep ``BOOKKEEPING_KEYS`` and the fields ``EVENT_FIELDS``
  declares for that event. A kept value must be a number, bool, ``None``, an
  identifier-shaped string (``ID_SHAPE``, or a field's own exact shape) or a list of those;
  anything else becomes ``"<redacted>"``. An undeclared event keeps the bookkeeping keys,
  and its name only if identifier-shaped.
- Standard-library records (Strands, mcp, uvicorn, botocore) keep the logger, level and
  message template without its arguments, plus the exception class. A record with no
  arguments may be a message the library formatted itself, so its text is withheld.
- Exception text and stack traces are always dropped.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from coa_common.logging import setup_logging
from structlog.typing import EventDict, WrappedLogger

REDACTED = "<redacted>"
WITHHELD = "<message withheld>"

# Keys every line may carry: added by the logging setup itself, or bound by
# server.run_workflow (run_id). ``_record``/``_from_structlog`` are structlog's
# own stdlib-bridge keys, removed before rendering.
BOOKKEEPING_KEYS = frozenset(
    {"event", "level", "timestamp", "logger", "logger_name", "run_id", "_record", "_from_structlog"}
)

# A short identifier: names, ids, sanitized routes, enum values, origins.
ID_SHAPE = re.compile(r"[A-Za-z0-9_.:/{}\-]{0,128}")
# The two fields composed in code rather than one identifier, each with its exact shape:
# ``server._sanitized_error`` (a class name, or a CoaApiError's method, sanitized route
# and status) and the idempotent POST retry's reason (a class name or an HTTP status).
_ERROR_SHAPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}(: [A-Z]{3,7} [A-Za-z0-9_./{}\-]{0,160} -> HTTP \d{1,3})?")
_REASON_SHAPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}|HTTP \d{3}( in progress)?")

_MAX_LIST_ITEMS = 200

# Every event this package logs, and the fields it may keep. A field not
# listed is dropped; an event not listed keeps only the bookkeeping keys.
EVENT_FIELDS: Mapping[str, frozenset[str]] = {
    # __main__
    "coa_workflow_agents_server_starting": frozenset({"host", "port", "cors_origins", "mutating_tools"}),
    "server_config_missing": frozenset({"missing"}),
    # server
    "inbound_request_context_missing_request_attribute": frozenset(),
    "run_workflow_rejected": frozenset({"reason", "error"}),
    "run_workflow_started": frozenset({"namespace_id"}),
    "run_workflow_timed_out": frozenset({"timeout_s", "namespace_id"}),
    "run_workflow_completed": frozenset({"outcome", "duration_s", "namespace_id"}),
    "run_workflow_failed": frozenset({"error", "namespace_id"}),
    # observability
    "tool_call_completed": frozenset({"agent", "tool", "outcome", "duration_s"}),
    "sub_agent_call_completed": frozenset({"sub_agent", "outcome", "duration_s"}),
    "run_token_usage": frozenset(
        {
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_read_input_tokens",
            "cache_write_input_tokens",
            "agents",
        }
    ),
    # intervention, run_context
    "mutating_tool_denied": frozenset({"tool_name"}),
    "progress_send_failed": frozenset({"error_type"}),
    "progress_sink_loop_closed": frozenset({"sent"}),
    # clients/coa_api_client
    "coa_api_error": frozenset({"method", "route", "status_code", "error_type"}),
    "coa_api_client.idempotent_post_retry": frozenset(
        {"route", "transient_failures", "in_progress_conflicts", "reason"}
    ),
    # tools/polling
    "poll_fetch_error_has_non_int_status_code": frozenset({"exception_type", "status_code_type"}),
    "poll_fetch_transient_failure": frozenset({"operation", "attempt", "error_type", "status_code", "route"}),
    "poll_timeout": frozenset({"operation", "timeout_s"}),
    "run_deadline_reached": frozenset({"operation", "timeout_s"}),
    # tools/_shared
    "collect_pages.non_string_next_token_ignored": frozenset({"token_type"}),
    "collect_pages.repeated_next_token_stopped": frozenset({"pages"}),
    "poll_initial_not_found_tolerated": frozenset({"route"}),
    # From coa_common.auth.token_authorizer (inbound token checks run in this
    # process). Their names are code constants, kept so an auth or key-fetch
    # failure is identifiable; their error/url values stay dropped.
    "JWT validation failed": frozenset(),
    "Failed to fetch JWKS": frozenset(),
    "Failed to fetch OIDC discovery document": frozenset(),
    "JWKS fetch failed recently; not retrying yet": frozenset({"retry_in_s"}),
}

_FIELD_SHAPES: Mapping[tuple[str, str], re.Pattern[str]] = {
    ("run_workflow_failed", "error"): _ERROR_SHAPE,
    ("coa_api_client.idempotent_post_retry", "reason"): _REASON_SHAPE,
}


def _safe_scalar(value: Any, shape: re.Pattern[str]) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str) and shape.fullmatch(value):
        return value
    return REDACTED


def _safe_value(value: Any, shape: re.Pattern[str]) -> Any:
    if isinstance(value, list | tuple | frozenset | set):
        items = sorted(value) if isinstance(value, frozenset | set) else list(value)
        if len(items) > _MAX_LIST_ITEMS:
            return REDACTED
        return [_safe_scalar(item, shape) for item in items]
    return _safe_scalar(value, shape)


def _foreign(event_dict: EventDict, record: logging.LogRecord) -> EventDict:
    """A stdlib record: logger, level and message template only, plus the exception class."""
    out: EventDict = {key: value for key, value in event_dict.items() if key in BOOKKEEPING_KEYS}
    template = record.msg if isinstance(record.msg, str) else type(record.msg).__name__
    # structlog clears the args on the record copy it hands the chain, after
    # formatting them into ``event``: an unchanged event means none were
    # substituted, so the message may be one the library formatted itself.
    had_args = event_dict.get("event") != template
    out["event"] = template if had_args else WITHHELD
    if record.exc_info and record.exc_info[0] is not None:
        out["exc_type"] = record.exc_info[0].__name__
    return out


def redact(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Keep only the bookkeeping keys and the event's declared, safely shaped fields."""
    record = event_dict.get("_record")
    if isinstance(record, logging.LogRecord) and not event_dict.get("_from_structlog", False):
        return _foreign(event_dict, record)
    event = event_dict.get("event")
    name = event if isinstance(event, str) else ""
    out: EventDict = {key: value for key, value in event_dict.items() if key in BOOKKEEPING_KEYS}
    out["event"] = name if name in EVENT_FIELDS or ID_SHAPE.fullmatch(name) else REDACTED
    for field in EVENT_FIELDS.get(name, frozenset()):
        if field in event_dict:
            shape = _FIELD_SHAPES.get((name, field), ID_SHAPE)
            out[field] = _safe_value(event_dict[field], shape)
    return out


def setup_service_logging(log_level: str) -> None:
    """The shared logging setup with :func:`redact` last on every line (structlog and stdlib)."""
    setup_logging(log_level, extra_processors=[redact])
