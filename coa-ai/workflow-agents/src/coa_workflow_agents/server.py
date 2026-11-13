# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""MCP server exposing the COA Workflow supervisor agent as one tool, ``run_workflow``.

Rules this module keeps:

- Auth: every ``run_workflow`` call verifies its bearer (``coa_common.auth``): an ID token
  whose audience is ``JWT_CLIENT_ID``, the COA web app client, the only client the REST
  API accepts. The server holds no credentials; every COA call forwards the caller's own
  token, so COA's policy decides what the caller may do (``auth/caller.py``).
- Mutations: there is one MCP tool, so client-side tool approval cannot gate individual
  COA operations. ``intervention.MutatingToolGate`` does, from the structured
  ``confirm_mutations`` argument.
- Threading: the supervisor and every tool below it are synchronous, so a run executes
  on a worker thread from a dedicated pool and never blocks the event loop.
- Deadlines: polls stop before the run deadline (``SCL_RUN_DEADLINE_S``, or the token's
  expiry when sooner). ``asyncio.wait_for`` is a backstop ``BACKSTOP_GRACE_S`` later. It
  cancels the wait, not the worker thread, which runs to completion in the background; the
  named pool keeps such threads visible.
- Logs: fail-closed (``log_policy.py``); errors log the exception class only.
"""

from __future__ import annotations

import asyncio
import contextvars
import re
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog
from coa_common.auth import TokenAuthorizer, build_token_authorizer
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware

from coa_workflow_agents.agents.supervisor import ALL_MUTATING_TOOL_NAMES, build_supervisor_agent
from coa_workflow_agents.auth.caller import (
    Caller,
    CallerTokenProvider,
    token_expired,
    token_remaining_s,
)
from coa_workflow_agents.clients.coa_api_client import CoaApiClient, CoaApiError, sanitized_route
from coa_workflow_agents.config import WorkflowAgentsConfig
from coa_workflow_agents.intervention import normalize_confirm_mutations
from coa_workflow_agents.observability import log_token_usage
from coa_workflow_agents.run_context import RunContext, make_progress_sink, reset_run, set_run

logger = structlog.get_logger(__name__)

config = WorkflowAgentsConfig()

mcp = FastMCP(
    name="coa-workflow-agents",
    host=config.mcp_host,
    port=config.mcp_port,
    # Stateful, so run_workflow can stream progress notifications.
    stateless_http=False,
    # FastMCP configures the standard library's logging at this level.
    log_level=config.log_level,
)

# What a browser MCP client sends and must read back (dev CORS only).
_CORS_METHODS = ("GET", "POST", "DELETE", "OPTIONS")
_CORS_HEADERS = ("Authorization", "Content-Type", "Mcp-Session-Id", "Mcp-Protocol-Version", "Last-Event-ID")


def build_http_app(cors_origins: Sequence[str]) -> Starlette:
    """The Streamable HTTP app, with CORS for exactly ``cors_origins`` when any are given.

    CORS only lets those loopback pages read responses; every call still verifies its token.
    """
    app = mcp.streamable_http_app()
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=list(_CORS_METHODS),
            allow_headers=list(_CORS_HEADERS),
            expose_headers=["Mcp-Session-Id"],
        )
    return app


# Named, so a thread orphaned by the backstop stays visible. Default bound, min(32, CPUs + 4);
# a call beyond it queues, and its run deadline counts down while it waits.
_THREAD_POOL = ThreadPoolExecutor(thread_name_prefix="run_workflow")

# Seconds between "still working" progress messages while a run is in flight.
HEARTBEAT_INTERVAL_S = 30.0

# Extra time past the run deadline before the outer wait_for gives up. Polls
# already stop RUN_DEADLINE_MARGIN_S before the deadline (tools/polling.py),
# so this only fires when the agent itself is stuck.
BACKSTOP_GRACE_S = 120.0

_inbound_token_authorizer: TokenAuthorizer | None = None
_inbound_token_authorizer_lock = threading.Lock()


def _get_inbound_token_authorizer() -> TokenAuthorizer:
    """The inbound verifier, built once: ID tokens whose ``aud`` is ``JWT_CLIENT_ID`` only.

    Double-checked lock: verification runs on worker threads, so two first calls can race.
    """
    global _inbound_token_authorizer
    if _inbound_token_authorizer is not None:
        return _inbound_token_authorizer
    with _inbound_token_authorizer_lock:
        if _inbound_token_authorizer is None:
            config.validate_required()
            _inbound_token_authorizer = build_token_authorizer(
                issuer=config.jwt_issuer_url,
                region=config.aws_region,
                client_id=[config.jwt_client_id],
                group_claim_name=config.group_claim_name,
                require_id_token=True,
            )
        return _inbound_token_authorizer


def _verify_inbound_caller(ctx: Context[ServerSession, Any, Any]) -> Caller:
    """Verify the caller's bearer token, failing closed.

    A missing header, a malformed or expired token, an access token, a wrong audience or a
    missing ``exp`` raises ``ValueError`` and the call is refused.
    """
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        logger.warning("inbound_request_context_missing_request_attribute")
    headers = getattr(request, "headers", None)
    auth_header = headers.get("authorization") if headers is not None else None
    if not auth_header:
        raise ValueError("Missing Authorization header — cannot verify the caller's identity.")
    result = _get_inbound_token_authorizer().validate(auth_header)
    expires_at = result.claims.get("exp")
    if not isinstance(expires_at, int | float):
        raise ValueError("Token rejected: no expiry claim")
    _, _, raw_token = auth_header.partition(" ")
    return Caller(
        email=result.email,
        groups=tuple(result.groups),
        # validate() accepted only "Bearer <token>", so the token follows the space.
        id_token=raw_token.strip(),
        expires_at=float(expires_at),
    )


_TOKEN_EXPIRING_MESSAGE = "Your COA token expires within a minute; fetch a fresh one and retry."


def _caller_client(caller: Caller) -> CoaApiClient:
    """A COA client for this call that forwards the caller's own token."""
    if token_expired(caller):
        raise ValueError(_TOKEN_EXPIRING_MESSAGE)
    return CoaApiClient(base_url=config.api_endpoint, token_provider=CallerTokenProvider(caller))


# COA's Uuid shape (common.smithy): lowercase UUID v4.
_NAMESPACE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def _validate_namespace_id(namespace_id: str) -> str:
    """``namespace_id`` stripped, or ``ValueError`` before any COA call. COA decides access."""
    value = namespace_id.strip()
    if not _NAMESPACE_ID.fullmatch(value):
        raise ValueError("namespace_id must be a COA namespace id (a lowercase UUID v4)")
    return value


def _namespace_line(namespace_id: str) -> str:
    """``Namespace: <id>``, the first line of every reply."""
    return f"Namespace: {namespace_id}"


def _run_supervisor_sync(
    caller: Caller,
    namespace_id: str,
    instruction: str,
    confirmed_tools: frozenset[str],
) -> str:
    """Build and invoke the supervisor on the worker thread, which owns the whole blocking chain."""
    with _caller_client(caller) as client:
        agents = build_supervisor_agent(client, namespace_id, config, confirmed_tools)
        try:
            result = agents.supervisor(instruction)
        finally:
            # A failed run still reports what it used.
            log_token_usage((agents.supervisor, *agents.sub_agents))
        return f"{_namespace_line(namespace_id)}\n\n{result}"


def _sanitized_error(e: BaseException) -> str:
    """The exception class for logs, never its message; a ``CoaApiError`` adds method, route and status."""
    if isinstance(e, CoaApiError):
        return f"CoaApiError: {e.method} {sanitized_route(e.path)} -> HTTP {e.status_code}"
    return type(e).__name__


async def _heartbeat(sink: Callable[[str], None], started: float, interval_s: float) -> None:
    """Send ``still working: <elapsed>`` to ``sink`` every ``interval_s`` until cancelled."""
    while True:
        await asyncio.sleep(interval_s)
        sink(f"still working: {time.monotonic() - started:.0f}s elapsed")


@mcp.tool()
async def run_workflow(
    ctx: Context[ServerSession, Any, Any],
    instruction: str,
    namespace_id: str,
    confirm_mutations: list[str] | None = None,
) -> str:
    """Ask the COA Workflow supervisor about one COA namespace, in natural language.

    For now it answers status questions: every source, induction job and proposal in the
    namespace, correlated by datasource ("what is the status of this namespace?"). It
    cannot change COA yet; it says so when asked.

    Every call works in ONE namespace, ``namespace_id``, with YOUR COA rights there: your own
    token is forwarded on every COA call, and COA denies what your role does not permit.

    Mutating operations are denied at the code level unless you name each one in
    ``confirm_mutations`` (or pass ``["*"]``). Text in ``instruction`` such as "yes, do it"
    authorizes nothing.

    Polls stop a minute before the run deadline (``SCL_RUN_DEADLINE_S``, or your token's
    expiry less a minute when sooner); a hung call fails two minutes after it. Progress
    notifications send a "still working" heartbeat every 30 s, and the state of any COA job a
    tool waits on. The first is ``run <run_id>``: quote that id when asking about the run.

    Example: instruction="What is the status of this namespace?", namespace_id="<uuid>".

    Args:
        ctx: MCP request context, used to verify the inbound token first.
        instruction: What you want done. Used as the task prompt only.
        namespace_id: The COA namespace (a lowercase UUID v4). The instruction cannot switch it.
        confirm_mutations: Mutating tool names you authorize for this run, or ``["*"]`` for
            all. Omit to run read-only. An unknown name is rejected with ``ValueError``.

    Returns:
        ``Namespace: <id>`` on the first line, then the supervisor's reply.

    Raises:
        ValueError: the token failed verification or expires within a minute,
            ``confirm_mutations`` named an unknown tool, ``namespace_id`` is malformed, the
            run raised (the error names its class only), or it hung past the backstop. COA
            operations already started are not cancelled.
    """
    caller, confirmed_tools, target_namespace = await _admit(ctx, confirm_mutations, namespace_id)
    where = _namespace_line(target_namespace)
    timeout_s = config.run_deadline_s
    backstop_s = timeout_s + BACKSTOP_GRACE_S
    # Bound before the worker's context is copied below, so every event of
    # the run (the worker thread and Strands' threads included) carries it.
    run_id = uuid.uuid4().hex
    with structlog.contextvars.bound_contextvars(run_id=run_id):
        logger.info("run_workflow_started", namespace_id=target_namespace)
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        outcome = "failed"

        async def send_progress(progress: float, message: str) -> None:
            await ctx.report_progress(progress, None, message)

        try:
            sink = make_progress_sink(loop, send_progress)
            # The run id, out of band: the caller can quote it, and the reply text stays as it was.
            sink(f"run {run_id}")
            # The run ends at the deadline or when the caller's token stops being
            # forwarded, whichever is first: a poll then ends as still_running with its
            # job id, rather than the next COA call being refused and the id lost.
            run_s = min(timeout_s, token_remaining_s(caller))
            # The worker thread runs in a copy of this context with the run set, so
            # every poll in the run sees the deadline and the sink (run_context.py).
            token = set_run(RunContext(deadline=started + run_s, progress=sink))
            try:
                worker_ctx = contextvars.copy_context()
            finally:
                reset_run(token)
            work = loop.run_in_executor(
                _THREAD_POOL,
                worker_ctx.run,
                _run_supervisor_sync,
                caller,
                target_namespace,
                instruction,
                confirmed_tools,
            )
            heartbeat = asyncio.create_task(_heartbeat(sink, started, HEARTBEAT_INTERVAL_S))
            try:
                result = await asyncio.wait_for(work, timeout=backstop_s)
            except TimeoutError as e:
                # The backstop's TimeoutError is raised from the CancelledError it
                # injected; ``work`` is then cancelled too, unless it completed in the
                # same loop iteration. Any other TimeoutError is the run's own.
                if not (work.cancelled() or isinstance(e.__cause__, asyncio.CancelledError)):
                    raise _run_failed(where, e, target_namespace) from e
                outcome = "timed_out"
                logger.error("run_workflow_timed_out", timeout_s=backstop_s, namespace_id=target_namespace)
                raise ValueError(
                    f"{where}. Workflow timed out after {backstop_s:.0f}s. Any COA operation "
                    "already started (e.g. an induction job) is still running on COA's side — "
                    "it was not cancelled. Check its status with a read-only instruction "
                    "(e.g. 'get the status of induction job <jobId>') rather than retrying blindly."
                ) from e
            except asyncio.CancelledError:
                # The client went away (or the server is shutting down): not a
                # failure of the run, and kept out of the success rate.
                outcome = "cancelled"
                raise
            except Exception as e:
                raise _run_failed(where, e, target_namespace) from e
            finally:
                heartbeat.cancel()
            outcome = "success"
            return result
        finally:
            # Exactly once per run, on every exit path.
            logger.info(
                "run_workflow_completed",
                outcome=outcome,
                duration_s=round(time.monotonic() - started, 3),
                namespace_id=target_namespace,
            )


async def _admit(
    ctx: Context[ServerSession, Any, Any], confirm_mutations: list[str] | None, namespace_id: str
) -> tuple[Caller, frozenset[str], str]:
    """Verify the caller and the arguments before any run, logging ``run_workflow_rejected`` on refusal.

    The log names the check that failed and the exception class, never the message.
    """
    reason = "auth"
    try:
        # Off the event loop: the first call, and each JWKS cache refresh, fetches keys over HTTP.
        caller = await asyncio.to_thread(_verify_inbound_caller, ctx)
        reason = "token_expiring"
        if token_expired(caller):
            raise ValueError(_TOKEN_EXPIRING_MESSAGE)
        reason = "confirm_mutations"
        confirmed_tools = normalize_confirm_mutations(confirm_mutations, ALL_MUTATING_TOOL_NAMES)
        reason = "namespace_id"
        return caller, confirmed_tools, _validate_namespace_id(namespace_id)
    except Exception as e:
        logger.warning("run_workflow_rejected", reason=reason, error=type(e).__name__)
        raise


def _run_failed(where: str, e: Exception, namespace_id: str) -> ValueError:
    """Log ``run_workflow_failed`` and build the caller's error.

    The class only in the log: a message may quote caller data
    or name AWS accounts, roles and ARNs, so the caller gets the class. The
    server's own refusal of a call queued behind busy workers until its token
    crossed the expiry margin is passed through whole (a constant, no caller
    data). A token that expires mid-run never gets here: the COA call fails
    inside a tool, and Strands hands the agent that tool's error result,
    which tells it to re-issue with a fresh token.
    """
    logger.error("run_workflow_failed", error=_sanitized_error(e), namespace_id=namespace_id)
    if isinstance(e, ValueError) and str(e) == _TOKEN_EXPIRING_MESSAGE:
        return ValueError(f"{where}. {_TOKEN_EXPIRING_MESSAGE}")
    return ValueError(
        f"{where}. Workflow failed ({type(e).__name__}). Any COA operation already "
        "started keeps running; check its status with a read-only instruction."
    )
