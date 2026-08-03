#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""BIRD Benchmark Runner for Context Ontology Accelerator.

Invokes the SCL AgentCore runtime with BIRD mini-dev questions and measures
execution rate, accuracy, latency, and confidence across strategies and models.

Accuracy is measured by executing gold SQL against the source database and
comparing result sets (order-insensitive, type-coerced).

Usage:
    python3 benchmark/runner.py
    python3 benchmark/runner.py --strategy ontop --model us.anthropic.claude-opus-4-6-v1
    python3 benchmark/runner.py --category aggregation --questions 0-49
    python3 benchmark/runner.py --parallel 8 --strategy best
    python3 benchmark/runner.py --skip-gold   # skip accuracy (no DB access)
    python3 benchmark/runner.py --no-cache    # re-execute all gold SQL (ignore cache)

Environment:
    AWS_REGION       (default: us-east-1)
    SCL_PREFIX       (default: coa)
    SCL_USERNAME     Cognito username (default: from Secrets Manager)
    SCL_PASSWORD     Cognito password (default: from Secrets Manager)
    BENCH_NS         Namespace override
    BENCH_DB_HOST    Gold DB host (default: localhost)
    BENCH_DB_PORT    Gold DB port (default: 15432, assumes SSM port-forward)
    BENCH_DB_NAME    Gold DB name (default: bird)
    BENCH_DB_USER    Gold DB user (default: bird_admin)
    BENCH_DB_PASS    Gold DB password
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

import boto3

# ─── Paths ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
DATASETS_DIR = SCRIPT_DIR / "datasets"
RESULTS_DIR = SCRIPT_DIR / "results"
DEFAULT_DATASET = DATASETS_DIR / "bird-mini-dev.json"

# ─── Configuration ────────────────────────────────────────────────────────────

REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
PREFIX = os.environ.get("SCL_PREFIX", "coa")
DEFAULT_NAMESPACE = os.environ.get("BENCH_NS", "385e5e21-ac50-48a1-b59b-bbba19f26a24")

# Gold database config (requires SSM port-forward to RDS)
# Password resolution order: BENCH_DB_PASS env var → Secrets Manager → None (will fail at connect)
_GOLD_DB_SECRET_NAME = os.environ.get("BENCH_DB_SECRET", "coa-integ-test-databases/postgresql/credentials")


def _resolve_gold_password() -> str:
    """Resolve gold DB password from env var or Secrets Manager."""
    env_pass = os.environ.get("BENCH_DB_PASS")
    if env_pass:
        return env_pass
    try:
        sm = boto3.client("secretsmanager", region_name=REGION)
        resp = sm.get_secret_value(SecretId=_GOLD_DB_SECRET_NAME)
        secret = json.loads(resp["SecretString"])
        return secret["password"]
    except Exception as e:
        print(
            f"⚠️  Could not resolve gold DB password from Secrets Manager ({_GOLD_DB_SECRET_NAME}): {e}", file=sys.stderr
        )
        print(
            "   Set BENCH_DB_PASS env var or ensure AWS credentials have secretsmanager:GetSecretValue permission.",
            file=sys.stderr,
        )
        return ""


_gold_db_config_cache: dict | None = None


def _get_gold_db_config() -> dict:
    """Lazily resolve gold DB config. Only calls Secrets Manager on first access."""
    global _gold_db_config_cache
    if _gold_db_config_cache is None:
        _gold_db_config_cache = {
            "host": os.environ.get("BENCH_DB_HOST", "localhost"),
            "port": int(os.environ.get("BENCH_DB_PORT", "15432")),
            "dbname": os.environ.get("BENCH_DB_NAME", "bird"),
            "user": os.environ.get("BENCH_DB_USER", "bird_admin"),
            "password": _resolve_gold_password(),
        }
    return _gold_db_config_cache


# ─── Aggregation Keywords for Category Classification ─────────────────────────

_AGG_KEYWORDS = ("COUNT(", "SUM(", "AVG(", "MAX(", "MIN(", "GROUP BY", "HAVING", "CASE WHEN", "IIF(")


def classify_category(gold_sql: str) -> str:
    """Classify a question as aggregation or non-aggregation based on gold SQL."""
    upper = gold_sql.upper()
    return "aggregation" if any(k in upper for k in _AGG_KEYWORDS) else "non-aggregation"


# ─── Gold SQL Execution & Comparison ──────────────────────────────────────────

_gold_conn = None
_gold_cache: dict[str, tuple[list | None, str | None]] = {}
_gold_lock = threading.Lock()
_gold_consecutive_failures: int = 0
_GOLD_MAX_CONSECUTIVE_FAILURES = 5  # stop retrying DB after N consecutive failures
_GOLD_CACHE_FILE = RESULTS_DIR / ".gold_cache.json"
_GOLD_CACHE_VERSION = "v2"  # bump when comparison logic or normalization changes


def _load_gold_cache() -> dict[str, tuple[list | None, str | None]]:
    """Load cached gold results from disk.

    Returns empty dict if cache is missing, corrupt, or from a different version.
    """
    if _GOLD_CACHE_FILE.exists():
        try:
            raw = json.loads(_GOLD_CACHE_FILE.read_text())
            # Version check — discard stale cache
            if isinstance(raw, dict) and raw.get("_version") != _GOLD_CACHE_VERSION:
                print(
                    f"  ⚠️  Gold cache version mismatch "
                    f"(got {raw.get('_version')!r}, want {_GOLD_CACHE_VERSION!r}) — discarding",
                    file=sys.stderr,
                )
                return {}
            entries = raw.get("entries", raw)  # support versioned and legacy format
            if isinstance(entries, dict):
                entries.pop("_version", None)
            cache = {}
            for sql, (rows, err) in entries.items():
                if rows is not None:
                    cache[sql] = ([tuple(r) for r in rows], err)
                else:
                    cache[sql] = (None, err)
            return cache
        except Exception:
            pass
    return {}


def _save_gold_cache():
    """Persist gold cache to disk with version stamp."""
    try:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        # Convert tuples to lists for JSON serialization
        serializable: dict = {"_version": _GOLD_CACHE_VERSION}
        for sql, (rows, err) in _gold_cache.items():
            if rows is not None:
                serializable[sql] = ([list(r) for r in rows], err)
            else:
                serializable[sql] = (None, err)
        _GOLD_CACHE_FILE.write_text(json.dumps(serializable))
    except Exception:
        pass


def _get_gold_conn():
    """Get or create a persistent PostgreSQL connection for gold SQL."""
    global _gold_conn
    try:
        import psycopg2
    except ImportError:
        raise RuntimeError("psycopg2 required for gold comparison. Install: pip install psycopg2-binary") from None

    if _gold_conn is None or _gold_conn.closed:
        config = _get_gold_db_config()
        _gold_conn = psycopg2.connect(
            host=config["host"],
            port=config["port"],
            dbname=config["dbname"],
            user=config["user"],
            password=config["password"],
            options="-c statement_timeout=30000",
            connect_timeout=10,
        )
        _gold_conn.autocommit = True
    return _gold_conn


def execute_gold_sql(gold_sql: str) -> tuple[list | None, str | None]:
    """Execute gold SQL against PostgreSQL. Returns (normalized_rows, error).

    Results are cached in-memory and on disk so repeated benchmark runs
    (e.g., benchmark-matrix with 15 strategy×model combos) don't re-execute
    the same 500 gold queries every time.

    Thread-safe: protected by _gold_lock.
    After _GOLD_MAX_CONSECUTIVE_FAILURES connection errors, stops retrying
    to avoid serial lock contention when the DB is unreachable.
    """
    global _gold_cache, _gold_conn, _gold_consecutive_failures
    import psycopg2

    with _gold_lock:
        # Check cache first
        if gold_sql in _gold_cache:
            return _gold_cache[gold_sql]

        # Circuit-breaker: stop retrying if DB is persistently down
        if _gold_consecutive_failures >= _GOLD_MAX_CONSECUTIVE_FAILURES:
            return None, "gold_circuit_open"

        try:
            conn = _get_gold_conn()
            cur = conn.cursor()
            cur.execute(gold_sql)
            rows = cur.fetchall()
            cur.close()
            normalized = sorted([tuple(str(v).strip() for v in row) for row in rows])
            _gold_cache[gold_sql] = (normalized, None)
            _gold_consecutive_failures = 0  # reset on success
            return normalized, None
        except psycopg2.extensions.QueryCanceledError:
            # Timeout is transient — do NOT cache
            return None, "gold_timeout"
        except Exception as e:
            _gold_consecutive_failures += 1
            try:
                if _gold_conn:
                    _gold_conn.close()
            except Exception:
                pass
            _gold_conn = None
            # Don't cache transient connection errors
            return None, f"gold_error: {str(e)[:200]}"


def compare_results(result_rows: list | None, gold_rows: list) -> bool:
    """Compare generated result rows against gold rows.

    Strict comparison:
    - Order-insensitive (rows sorted before comparison)
    - Type-coerced (all values stringified and stripped)
    - Exact column count match required
    - 1% numeric tolerance for single-value scalar results only
    """
    if not result_rows and not gold_rows:
        return True
    if not result_rows or not gold_rows:
        return False

    try:
        # Normalize result_rows from the API response
        if isinstance(result_rows[0], dict):
            gen_raw = [tuple(str(v).strip() for v in row.values()) for row in result_rows]
        elif isinstance(result_rows[0], (list, tuple)):
            gen_raw = [tuple(str(v).strip() for v in row) for row in result_rows]
        else:
            gen_raw = [(str(v).strip(),) for v in result_rows]
    except Exception:
        return False

    gold_norm = gold_rows  # already normalized
    gold_cols = len(gold_norm[0]) if gold_norm else 0
    gen_cols = len(gen_raw[0]) if gen_raw else 0

    # Exact match (same column count)
    if gen_cols == gold_cols and sorted(gen_raw) == sorted(gold_norm):
        return True

    # Numeric tolerance for single-value scalar results only
    if len(gen_raw) == 1 and len(gold_norm) == 1 and gen_cols == gold_cols == 1:
        try:
            gen_val = float(gen_raw[0][0])
            gold_val = float(gold_norm[0][0])
            if gold_val == 0:
                return gen_val == 0
            return abs(gen_val - gold_val) / abs(gold_val) < 0.01  # 1% tolerance
        except (ValueError, TypeError):
            pass

    return False


# ─── Auth & Invocation ────────────────────────────────────────────────────────


def _resolve_runtime_arn(ssm) -> str:
    """Resolve runtime ARN from SSM parameter store."""
    return ssm.get_parameter(Name=f"/{PREFIX}/serve/runtime-arn")["Parameter"]["Value"]


def _resolve_credentials(ssm) -> tuple[str, str]:
    """Resolve Cognito credentials from Secrets Manager or env."""
    username = os.environ.get("SCL_USERNAME", "")
    password = os.environ.get("SCL_PASSWORD", "")
    if username and password:
        return username, password
    sm = boto3.client("secretsmanager", region_name=REGION)
    secret = json.loads(sm.get_secret_value(SecretId=f"{PREFIX}-dev-integ-test-user")["SecretString"])
    return secret["username"], secret["password"]


def _get_access_token(ssm) -> str:
    """Authenticate with Cognito and return the IdToken.

    The serve runtime's JWT authorizer validates the ``aud`` claim, which is
    only present on the ID token — an access token yields HTTP 401.
    """
    username, password = _resolve_credentials(ssm)
    client_id = ssm.get_parameter(Name=f"/{PREFIX}/userpool-client-id")["Parameter"]["Value"]
    cognito = boto3.client("cognito-idp", region_name=REGION)
    auth = cognito.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
        ClientId=client_id,
    )
    return auth["AuthenticationResult"]["IdToken"]


def _build_invoke_url(runtime_arn: str) -> str:
    """Build AgentCore invocation URL."""
    encoded = urllib.parse.quote(runtime_arn, safe="")
    return f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{encoded}/invocations?qualifier=DEFAULT"


def _consume_sse(resp) -> dict:
    """Consume an AgentCore SSE response incrementally, stopping at the terminal
    event.

    Since the WebSocket-to-SSE swap the serve runtime streams progress events
    then a terminal ``done``/``error`` event, but KEEPS THE CONNECTION OPEN
    afterwards — so ``resp.read()`` to EOF blocks until the socket timeout.
    Falls back to the legacy double-encoding / ``Decimal('..')`` repair for
    non-streaming single-frame responses.
    """
    import ast

    for raw_line in resp:
        line = raw_line.decode("utf-8", "replace") if isinstance(raw_line, (bytes, bytearray)) else raw_line
        line = line.strip()
        if not line:
            continue
        data_str = line[len("data: ") :] if line.startswith("data: ") else line
        try:
            event = json.loads(data_str)
        except (json.JSONDecodeError, ValueError):
            cleaned = re.sub(r"Decimal\('([^']*)'\)", r"\1", data_str)
            try:
                event = ast.literal_eval(cleaned)
            except (ValueError, SyntaxError):
                continue
        if isinstance(event, str):
            try:
                event = json.loads(event)
            except (json.JSONDecodeError, ValueError):
                continue
        if not isinstance(event, dict):
            continue

        ftype = event.get("type")
        if ftype in ("step", "token", "stage_start"):
            continue
        if ftype == "done" and isinstance(event.get("payload"), dict):
            return {"result": event["payload"].get("result"), "requestId": event.get("requestId")}
        if ftype == "error" and isinstance(event.get("payload"), dict):
            return {**event["payload"], "requestId": event.get("requestId")}
        if "result" in event or "error" in event or "statusCode" in event:
            return event
        if any(k in event for k in ("resultRows", "result_rows")):
            return event
    return {"error": "no terminal event in stream", "statusCode": 504}


class _TokenManager:
    """Thread-safe token manager with automatic refresh."""

    TOKEN_LIFETIME_S = 3600  # Cognito tokens last 1 hour
    REFRESH_BUFFER_S = 600  # Refresh 10 min before expiry

    def __init__(self, ssm):
        self._ssm = ssm
        self._token: str = ""
        self._acquired_at: float = 0
        self._lock = threading.Lock()

    def _refresh(self):
        """Refresh the token (caller must hold lock)."""
        self._token = _get_access_token(self._ssm)
        self._acquired_at = time.time()

    def get_token(self) -> str:
        """Get a valid token, refreshing if needed. Thread-safe."""
        with self._lock:
            age = time.time() - self._acquired_at
            if not self._token or age > (self.TOKEN_LIFETIME_S - self.REFRESH_BUFFER_S):
                self._refresh()
            return self._token


def invoke_query(
    url: str,
    token_manager: _TokenManager | str,
    question: str,
    namespace: str,
    *,
    tier: int = 2,
    strategy: str | None = None,
    model: str | None = None,
    max_retries: int = 2,
    timeout_s: int = 120,
) -> dict:
    """Invoke a single query against AgentCore. Returns parsed response or error dict.

    Retries on:
    - HTTP 401 (expired token — forces token refresh)
    - HTTP 429 (rate limit — exponential backoff)
    - HTTP 5xx (transient server error)
    - Timeout (once)
    """
    options: dict = {"tierOverride": tier}
    if strategy:
        options["strategy"] = strategy
    if model:
        options["modelId"] = model

    payload = json.dumps(
        {
            "query": question,
            "namespace": namespace,
            "options": options,
            "profile": {},
            "requestId": str(uuid.uuid4()),
        }
    ).encode()

    start = time.perf_counter()
    last_error = None

    # Unique AgentCore session id per request (>=33 chars) so parallel questions
    # run on independent sessions instead of serializing onto one sticky session.
    rt_session_id = f"bench-{uuid.uuid4().hex}"

    for attempt in range(max_retries + 1):
        # Get token fresh on each attempt (handles 401 refresh)
        if isinstance(token_manager, _TokenManager):
            if attempt > 0 and last_error and "401" in str(last_error):
                # Force refresh on 401 retry
                with token_manager._lock:
                    token_manager._refresh()
            token = token_manager.get_token()
        else:
            token = token_manager

        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": rt_session_id,
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                # Serve streams SSE and keeps the connection open after the
                # terminal event — consume incrementally and stop there instead
                # of read()-ing to EOF (which blocks until the socket timeout).
                data = _consume_sse(resp)
                latency_ms = int((time.perf_counter() - start) * 1000)
                data["_latency_ms"] = latency_ms
                return data
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 401 and attempt < max_retries:
                continue  # retry with refreshed token
            if e.code == 429 and attempt < max_retries:
                time.sleep(2**attempt)  # exponential backoff
                continue
            if e.code >= 500 and attempt < max_retries:
                time.sleep(1)
                continue
            latency_ms = int((time.perf_counter() - start) * 1000)
            body = e.read().decode() if e.fp else ""
            return {"error": f"HTTP {e.code}", "detail": body[:500], "_latency_ms": latency_ms}
        except (TimeoutError, OSError) as e:
            last_error = e
            if attempt < max_retries:
                continue  # retry timeout once
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {"error": f"timeout ({timeout_s}s)", "_latency_ms": latency_ms}
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {"error": str(e), "_latency_ms": latency_ms}

    # Should not reach here, but just in case
    latency_ms = int((time.perf_counter() - start) * 1000)
    return {"error": f"max_retries_exceeded: {last_error}", "_latency_ms": latency_ms}


# ─── Result Extraction ────────────────────────────────────────────────────────


def extract_metrics(resp: dict) -> dict:
    """Extract key metrics from a response."""
    metrics = {
        "executed": False,
        "error": None,
        "latency_ms": resp.get("_latency_ms", 0),
        "model_id": None,
        "confidence": None,
        "tier": None,
        "row_count": None,
        "result_rows": None,
        "query_used": None,
    }

    if "error" in resp and "result" not in resp:
        metrics["error"] = resp.get("message", resp.get("error", "unknown"))
        return metrics

    # Navigate response structure (AgentCore wraps in {result: ..., requestId: ...})
    result = resp.get("result", resp)
    if isinstance(result, dict):
        metrics["tier"] = result.get("tier")
        rows = result.get("resultRows") or result.get("result_rows") or []
        metrics["row_count"] = len(rows)
        metrics["result_rows"] = rows
        metrics["query_used"] = result.get("queryUsed") or result.get("query_used")

        conf = result.get("confidence")
        if isinstance(conf, dict):
            metrics["confidence"] = conf.get("score")
        elif isinstance(conf, (int, float)):
            metrics["confidence"] = conf

        meta = result.get("metadata", {})
        if isinstance(meta, dict):
            metrics["model_id"] = meta.get("modelId")

        # Consider executed if we got a tier back and no error
        if result.get("tier") is not None or metrics["row_count"] > 0:
            metrics["executed"] = True
    else:
        metrics["error"] = "unexpected_response_format"

    return metrics


# ─── Question Selection ───────────────────────────────────────────────────────


def parse_question_range(spec: str, total: int) -> list[int]:
    """Parse question range spec: 'all', '0-49', '10,20,30', '0-9,50-59'."""
    if spec.strip().lower() == "all":
        return list(range(total))
    indices = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            indices.extend(range(int(start), min(int(end) + 1, total)))
        else:
            idx = int(part)
            if idx < total:
                indices.append(idx)
    return sorted(set(indices))


def filter_by_category(questions: list[dict], category: str | None) -> list[dict]:
    """Filter questions by category (aggregation / non-aggregation)."""
    if not category:
        return questions
    return [q for q in questions if classify_category(q.get("SQL", "")) == category]


# ─── Parallel Execution ───────────────────────────────────────────────────────


def run_benchmark(
    questions: list[dict],
    url: str,
    token: _TokenManager | str,
    namespace: str,
    *,
    tier: int = 2,
    strategy: str | None = None,
    model: str | None = None,
    parallel: int = 4,
    skip_gold: bool = False,
    no_cache: bool = False,
) -> list[dict]:
    """Run benchmark on a list of questions, return per-question results."""
    global _gold_cache

    results = [None] * len(questions)
    completed = 0
    total = len(questions)
    correct = 0

    # Pre-validate gold DB connection and load cache
    gold_available = False
    if not skip_gold:
        # Load cached gold results from prior runs.
        # Safe: this runs BEFORE the thread pool is created, so no concurrent access.
        with _gold_lock:
            if no_cache:
                _gold_cache = {}
                if _GOLD_CACHE_FILE.exists():
                    _GOLD_CACHE_FILE.unlink()
                    print("  🗑  Gold cache cleared (--no-cache)")
            else:
                _gold_cache = _load_gold_cache()

        # Check if cache covers all questions (no DB needed)
        all_cached = all(q.get("SQL", "") in _gold_cache for q in questions if q.get("SQL"))
        if all_cached and _gold_cache:
            gold_available = True
            print(f"  ✔ Gold cache complete ({len(_gold_cache)} entries) — no DB connection needed")
        else:
            # Need DB connection — try connecting
            try:
                _get_gold_conn()
                gold_available = True
                cached_count = sum(1 for q in questions if q.get("SQL", "") in _gold_cache)
                if _gold_cache:
                    print(f"  ✔ Gold DB connected ({cached_count}/{len(questions)} already cached)")
            except Exception as e:
                # Try auto-starting the tunnel
                tunnel_script = SCRIPT_DIR / "tunnel.sh"
                if tunnel_script.exists():
                    print("\n  ⚠ Gold DB unavailable. Attempting to start tunnel...")
                    try:
                        result = subprocess.run(
                            ["bash", str(tunnel_script), "--background"],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        if result.returncode == 0:
                            # Wait and retry connection
                            time.sleep(3)
                            try:
                                _get_gold_conn()
                                gold_available = True
                                print("  ✔ Tunnel auto-started and connected")
                            except Exception:
                                print("  ⚠ Tunnel started but DB connection failed. Running without accuracy.")
                        else:
                            print(f"  ⚠ Tunnel failed: {result.stderr[:200]}")
                            print("    Running without accuracy.")
                    except Exception as te:
                        print(f"  ⚠ Could not start tunnel ({te}). Running without accuracy.")
                else:
                    print(f"\n  ⚠ Gold DB unavailable ({e}). Running without accuracy.")
                    print("    Start tunnel: make benchmark-tunnel")

    def _run_one(idx: int, q: dict) -> tuple[int, dict]:
        resp = invoke_query(
            url,
            token,
            q["question"],
            namespace,
            tier=tier,
            strategy=strategy,
            model=model,
        )
        metrics = extract_metrics(resp)
        metrics["question_id"] = q.get("question_id")
        metrics["question"] = q["question"]
        metrics["db_id"] = q.get("db_id")
        metrics["difficulty"] = q.get("difficulty")
        metrics["category"] = classify_category(q.get("SQL", ""))
        metrics["gold_sql"] = q.get("SQL")
        metrics["accurate"] = None  # unknown until compared

        # Gold SQL comparison
        if gold_available and metrics["executed"]:
            gold_sql = q.get("SQL", "")
            if gold_sql:
                gold_rows, gold_err = execute_gold_sql(gold_sql)
                if gold_err:
                    metrics["gold_error"] = gold_err
                else:
                    metrics["accurate"] = compare_results(metrics["result_rows"], gold_rows)

        # Don't store full result_rows in final output (too large)
        row_count = metrics["row_count"]
        metrics.pop("result_rows", None)
        metrics["row_count"] = row_count

        return idx, metrics

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = {executor.submit(_run_one, i, q): i for i, q in enumerate(questions)}
        for future in concurrent.futures.as_completed(futures):
            try:
                idx, metrics = future.result()
            except Exception as exc:
                # Handle unexpected errors in worker threads
                idx = futures[future]
                metrics = {
                    "question_id": questions[idx].get("question_id"),
                    "question": questions[idx].get("question", ""),
                    "db_id": questions[idx].get("db_id"),
                    "difficulty": questions[idx].get("difficulty"),
                    "category": classify_category(questions[idx].get("SQL", "")),
                    "gold_sql": questions[idx].get("SQL"),
                    "executed": False,
                    "error": f"worker_exception: {exc}",
                    "latency_ms": 0,
                    "model_id": None,
                    "confidence": None,
                    "tier": None,
                    "row_count": None,
                    "query_used": None,
                    "accurate": None,
                }
            results[idx] = metrics
            completed += 1
            if metrics.get("accurate"):
                correct += 1
            # Progress indicator with running accuracy
            if metrics["accurate"] is True:
                status = "✓"
            elif metrics["accurate"] is False:
                status = "≠"
            elif metrics.get("executed"):
                status = "○"
            else:
                status = "✗"
            compared = sum(1 for r in results if r and r.get("accurate") is not None)
            acc_str = f"{100 * correct / compared:.1f}% acc" if compared > 0 else "..."
            qid = metrics.get("question_id", "?")
            latency = metrics.get("latency_ms", 0)
            sys.stdout.write(f"\r  [{completed}/{total}] {acc_str} | {status} q{qid} ({latency}ms)   ")
            sys.stdout.flush()

    print()  # newline after progress
    return results


# ─── Reporting ────────────────────────────────────────────────────────────────


def print_summary(results: list[dict], strategy: str | None, model: str | None):
    """Print a summary table to stdout."""
    total = len(results)
    executed = sum(1 for r in results if r["executed"])
    errors = sum(1 for r in results if r["error"])
    accurate = sum(1 for r in results if r.get("accurate") is True)
    inaccurate = sum(1 for r in results if r.get("accurate") is False)
    has_accuracy = accurate + inaccurate > 0
    avg_latency = sum(r["latency_ms"] for r in results) / total if total else 0
    avg_confidence = 0.0
    conf_count = 0
    for r in results:
        if r["confidence"] is not None:
            avg_confidence += r["confidence"]
            conf_count += 1
    avg_confidence = avg_confidence / conf_count if conf_count else 0.0

    # By category
    categories = {}
    for r in results:
        cat = r.get("category", "unknown")
        if cat not in categories:
            categories[cat] = {"total": 0, "executed": 0, "errors": 0, "accurate": 0, "compared": 0}
        categories[cat]["total"] += 1
        if r["executed"]:
            categories[cat]["executed"] += 1
        if r["error"]:
            categories[cat]["errors"] += 1
        if r.get("accurate") is True:
            categories[cat]["accurate"] += 1
        if r.get("accurate") is not None:
            categories[cat]["compared"] += 1

    # By difficulty
    difficulties = {}
    for r in results:
        diff = r.get("difficulty", "unknown")
        if diff not in difficulties:
            difficulties[diff] = {"total": 0, "executed": 0, "accurate": 0, "compared": 0}
        difficulties[diff]["total"] += 1
        if r["executed"]:
            difficulties[diff]["executed"] += 1
        if r.get("accurate") is True:
            difficulties[diff]["accurate"] += 1
        if r.get("accurate") is not None:
            difficulties[diff]["compared"] += 1

    print("\n" + "═" * 60)
    print("  BENCHMARK RESULTS")
    print("═" * 60)
    print(f"  Strategy:    {strategy or '(default)'}")
    print(f"  Model:       {model or '(default)'}")
    print(f"  Questions:   {total}")
    print(f"  Executed:    {executed}/{total} ({100 * executed / total:.1f}%)")
    if has_accuracy:
        print(f"  Accurate:    {accurate}/{executed} ({100 * accurate / executed:.1f}% of executed)")
        print(f"  EX Accuracy: {accurate}/{total} ({100 * accurate / total:.1f}% of total) ← BIRD metric")
    print(f"  Errors:      {errors}/{total} ({100 * errors / total:.1f}%)")
    print(f"  Avg Latency: {avg_latency:.0f}ms")
    print(f"  Avg Confid.: {avg_confidence:.3f}")

    print("\n  By Category:")
    for cat, stats in sorted(categories.items()):
        exec_pct = 100 * stats["executed"] / stats["total"] if stats["total"] else 0
        if has_accuracy and stats["compared"] > 0:
            acc_pct = 100 * stats["accurate"] / stats["total"] if stats["total"] else 0
            print(
                f"    {cat:20s}  exec {stats['executed']:3d}/{stats['total']:3d} ({exec_pct:.0f}%)  "
                f"acc {stats['accurate']:3d}/{stats['total']:3d} ({acc_pct:.0f}%)"
            )
        else:
            print(f"    {cat:20s}  exec {stats['executed']:3d}/{stats['total']:3d} ({exec_pct:.0f}%)")

    print("\n  By Difficulty:")
    for diff in ["simple", "moderate", "challenging"]:
        if diff in difficulties:
            stats = difficulties[diff]
            exec_pct = 100 * stats["executed"] / stats["total"] if stats["total"] else 0
            if has_accuracy and stats["compared"] > 0:
                acc_pct = 100 * stats["accurate"] / stats["total"] if stats["total"] else 0
                print(
                    f"    {diff:20s}  exec {stats['executed']:3d}/{stats['total']:3d} ({exec_pct:.0f}%)  "
                    f"acc {stats['accurate']:3d}/{stats['total']:3d} ({acc_pct:.0f}%)"
                )
            else:
                print(f"    {diff:20s}  exec {stats['executed']:3d}/{stats['total']:3d} ({exec_pct:.0f}%)")

    print("═" * 60)


def save_results(
    results: list[dict],
    strategy: str | None,
    model: str | None,
    metadata: dict,
) -> Path:
    """Save results to a timestamped JSON file."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    strat_part = strategy or "default"
    model_part = (model or "default").split(".")[-1].replace(":", "-").replace("/", "-")
    filename = f"{ts}_{strat_part}_{model_part}.json"
    path = RESULTS_DIR / filename

    # Compute summary stats
    total = len(results)
    executed = sum(1 for r in results if r["executed"])
    accurate = sum(1 for r in results if r.get("accurate") is True)
    has_accuracy = any(r.get("accurate") is not None for r in results)

    output = {
        "metadata": metadata,
        "summary": {
            "total": total,
            "executed": executed,
            "execution_rate": round(100 * executed / total, 1) if total else 0,
            "errors": sum(1 for r in results if r["error"]),
            "avg_latency_ms": round(sum(r["latency_ms"] for r in results) / total, 0) if total else 0,
            **({"accurate": accurate, "accuracy_pct": round(100 * accurate / total, 1)} if has_accuracy else {}),
        },
        "results": results,
    }
    path.write_text(json.dumps(output, indent=2, default=str))
    print(f"\n  Results saved: {path.relative_to(SCRIPT_DIR)}")
    return path


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="BIRD Benchmark Runner for SCL")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE, help="Namespace ID")
    parser.add_argument("--tier", type=int, default=2, help="Tier override (default: 2)")
    parser.add_argument("--strategy", default=None, help="Strategy: ontop|nl_to_sql|best|ontop_first|nl_to_sql_first")
    parser.add_argument("--model", default=None, help="Model ID override (e.g., us.anthropic.claude-opus-4-8)")
    parser.add_argument("--category", default=None, help="Filter: aggregation|non-aggregation")
    parser.add_argument("--questions", default="all", help="Question range: all|0-49|10,20,30")
    parser.add_argument("--parallel", type=int, default=4, help="Concurrent requests (default: 4)")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="Path to dataset JSON")
    parser.add_argument("--skip-gold", action="store_true", help="Skip gold SQL comparison (no accuracy)")
    parser.add_argument("--no-cache", action="store_true", help="Ignore gold cache (re-execute all gold SQL)")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    # Validate namespace
    if not args.namespace or args.namespace.strip() == "":
        print("ERROR: Namespace is required. Set BENCH_NS env var or pass --namespace.")
        sys.exit(1)

    # Load dataset
    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        # Try relative to datasets dir
        dataset_path = DATASETS_DIR / f"{args.dataset}.json"
    if not dataset_path.exists():
        print(f"ERROR: Dataset not found: {args.dataset}")
        sys.exit(1)

    with open(dataset_path) as f:
        all_questions = json.load(f)
    print(f"  Dataset: {dataset_path.name} ({len(all_questions)} questions)")

    # Filter by category
    questions = filter_by_category(all_questions, args.category)
    if args.category:
        print(f"  Category filter: {args.category} → {len(questions)} questions")

    # Select question range
    indices = parse_question_range(args.questions, len(questions))
    questions = [questions[i] for i in indices]
    print(f"  Selected: {len(questions)} questions")

    if not questions:
        print("ERROR: No questions selected")
        sys.exit(1)

    # Auth
    print("\n  Authenticating...")
    ssm = boto3.client("ssm", region_name=REGION)
    try:
        runtime_arn = _resolve_runtime_arn(ssm)
        token_mgr = _TokenManager(ssm)
        token_mgr.get_token()  # validate credentials upfront
    except Exception as e:
        print(f"ERROR: Auth failed: {e}")
        sys.exit(1)

    url = _build_invoke_url(runtime_arn)

    # ─── Pre-run summary & confirmation ───────────────────────────────────
    est_per_question_s = 18  # ~18s avg from prior runs
    est_total_min = (len(questions) * est_per_question_s) / args.parallel / 60

    print(f"\n{'═' * 60}")
    print("  BIRD Benchmark — Pre-Run Summary")
    print(f"{'═' * 60}")
    print(f"  Dataset:        {dataset_path.name} ({len(all_questions)} total)")
    print(f"  Questions:      {len(questions)} selected")
    if args.category:
        print(f"  Category:       {args.category}")
    print(f"  Strategy:       {args.strategy or '(default)'}")
    print(f"  Model:          {args.model or '(default)'}")
    print(f"  Tier:           {args.tier}")
    print(f"  Parallel:       {args.parallel}")
    print(f"  Gold compare:   {'disabled' if args.skip_gold else 'enabled'}")
    print(f"  Runtime:        {runtime_arn.split('/')[-1]}")
    print(f"  Namespace:      {args.namespace[:20]}...")
    print(f"{'─' * 60}")
    print(
        f"  Est. duration:  ~{est_total_min:.0f} min"
        f" ({len(questions)} × ~{est_per_question_s}s / {args.parallel} parallel)"
    )
    print(f"  Est. API calls: {len(questions)}")
    print(f"{'═' * 60}")

    if not args.yes:
        try:
            confirm = input("\n  Proceed? [Y/n] ").strip().lower()
            if confirm and confirm not in ("y", "yes"):
                print("  Aborted.")
                sys.exit(0)
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(0)

    print("\n  Running...")

    # Execute
    t_start = time.time()
    results = run_benchmark(
        questions,
        url,
        token_mgr,
        args.namespace,
        tier=args.tier,
        strategy=args.strategy,
        model=args.model,
        parallel=args.parallel,
        skip_gold=args.skip_gold,
        no_cache=args.no_cache,
    )
    elapsed = time.time() - t_start

    # Summary
    print_summary(results, args.strategy, args.model)
    print(f"\n  Total time: {elapsed:.1f}s")

    # Save
    metadata = {
        "timestamp": datetime.now(UTC).isoformat(),
        "dataset": dataset_path.name,
        "namespace": args.namespace,
        "strategy": args.strategy,
        "model": args.model,
        "tier": args.tier,
        "category_filter": args.category,
        "question_range": args.questions,
        "parallel": args.parallel,
        "total_seconds": round(elapsed, 1),
        "gold_comparison": not args.skip_gold,
    }
    save_results(results, args.strategy, args.model, metadata)

    # Persist gold cache for future runs
    if _gold_cache and not args.skip_gold:
        _save_gold_cache()
        print(f"  Gold cache saved ({len(_gold_cache)} entries)")


if __name__ == "__main__":
    main()
