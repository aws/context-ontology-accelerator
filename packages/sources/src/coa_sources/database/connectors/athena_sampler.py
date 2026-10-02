# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Distinct-value sampling for Glue Data Catalog tables via Athena.

Glue-catalog sources have no live JDBC connection — their data is queried
through Athena (AwsDataCatalog). This module provides the same enum-value
sampling the JDBC dialects do (:class:`dialects.InformationSchemaDialect.
fetch_distinct_values`), but issued as Athena SQL:

  1. ``SELECT COUNT(*), COUNT(DISTINCT col)`` over non-null/non-empty values —
     gates on bounded distinct count AND average per-value repetition
     (``total >= MIN_REPETITION_FACTOR * distinct``) so unique-identifier
     columns are excluded even in small tables.
  2. ``SELECT DISTINCT col ... LIMIT max_distinct`` for columns that pass.

Best-effort throughout: any Athena error (missing workgroup, LF denial,
timeout) leaves the column unsampled and never aborts discovery.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

from .dialects import (
    MAX_ENUM_DISTINCT,
    MIN_REPETITION_FACTOR,
    PROBE_MAX_ROWS,
    probe_gate_sql,
    probe_values_sql,
    values_look_categorical,
)

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back on bad input.

    A misconfigured value must never break module import or hand
    ``ThreadPoolExecutor`` a non-positive ``max_workers``: non-numeric falls back
    to ``default``, and the result is floored at 1.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("invalid_int_env_ignored", extra={"var": name, "value": raw, "fallback": default})
        return default


AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Max Athena queries in flight at once across the whole discovery. Sampling is
# I/O-bound (blocking boto3 submit + 1s poll-waits per query); boto3 has no
# native async, so we fan out with a ThreadPoolExecutor — the GIL releases
# during the socket wait, so threads overlap the poll-waits (the entire win).
# Athena enforces a per-account concurrent-DML limit (default ~20-25), and each
# thread holds one column across BOTH of its queries, so this cap bounds total
# concurrent queries.
# ponytail: fixed cap tuned under the default Athena DML limit; raise the env
# var if the account has a higher concurrent-query quota.
ATHENA_SAMPLING_CONCURRENCY = _int_env("ATHENA_SAMPLING_CONCURRENCY", 16)

# String/categorical types worth sampling (Glue/Trino type names). Numeric,
# temporal, and binary columns are excluded — their values are not useful enum
# hints and are more likely high-cardinality/PII. Mirrors
# JdbcConnector._SAMPLEABLE_TYPE_PREFIXES.
_SAMPLEABLE_TYPE_PREFIXES = ("char", "varchar", "text", "string")

# Athena query settling — discovery runs in a 15-min Lambda; keep the per-query
# wait short so a slow/blocked column can't dominate the scan.
_POLL_INTERVAL_S = 1.0
_QUERY_TIMEOUT_S = 30
_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}
# get_query_results page size — covers the COUNT row and the DISTINCT LIMIT
# (<= MAX_ENUM_DISTINCT) plus Athena's header row.
_RESULTS_PAGE_SIZE = 200


def _quote_ident(ident: str) -> str:
    """Double-quote an Athena identifier, escaping embedded quotes."""
    return '"' + ident.replace('"', '""') + '"'


class AthenaSampler:
    """Samples distinct categorical values from Glue tables via Athena.

    One instance per discovery; reuses a boto3 Athena client. ``workgroup`` and
    ``output_location`` come from env (set by the sources stack); when neither is
    available sampling is skipped (returns ``{}``) rather than failing.
    """

    def __init__(
        self,
        region: str = AWS_REGION,
        workgroup: str | None = None,
        output_location: str | None = None,
        catalog: str = "AwsDataCatalog",
    ):
        """Configure the sampler's Athena target and query settings.

        Args:
            region: AWS region for the Athena client.
            workgroup: Athena workgroup to run queries in; falls back to the
                ``ATHENA_WORKGROUP`` env var.
            output_location: S3 URI for query results; falls back to a path
                derived from the ``ATHENA_SPILL_BUCKET`` env var.
            catalog: Glue data catalog name to query against.
        """
        self._region = region
        self._workgroup = workgroup or os.getenv("ATHENA_WORKGROUP", "")
        self._output_location = output_location or (
            f"s3://{os.getenv('ATHENA_SPILL_BUCKET')}/enum-sampling/" if os.getenv("ATHENA_SPILL_BUCKET") else ""
        )
        self._catalog = catalog
        self._client = None

    def _athena(self):
        if self._client is None:
            self._client = boto3.client("athena", region_name=self._region)
        return self._client

    @property
    def enabled(self) -> bool:
        """Sampling requires a workgroup or an explicit output location."""
        return bool(self._workgroup or self._output_location)

    def sample_columns(
        self,
        database: str,
        table: str,
        columns: list[tuple[str, str]],
        *,
        max_distinct: int = MAX_ENUM_DISTINCT,
    ) -> dict[str, list[str]]:
        """Sample enum-like values for one table's ``(name, data_type)`` columns.

        Sync entry point (unchanged signature). Delegates to :meth:`sample_all`
        so the per-column sampling fans out on the shared thread pool. Returns
        ``{column_name: [values]}`` only for columns that pass the cardinality +
        repetition gate. Non-string columns are skipped up front.
        """
        if not self.enabled:
            logger.info("athena_sampling_skipped_no_workgroup", extra={"database": database, "table": table})
            return {}
        by_table = self.sample_all(database, [(table, columns)], max_distinct=max_distinct)
        return by_table.get(table, {})

    def sample_all(
        self,
        database: str,
        tables: list[tuple[str, list[tuple[str, str]]]],
        *,
        max_distinct: int = MAX_ENUM_DISTINCT,
        max_workers: int | None = None,
    ) -> dict[str, dict[str, list[str]]]:
        """Sample enum-like values for many tables in parallel.

        ``tables`` is ``[(table_name, [(col_name, data_type), ...]), ...]``.
        Fans out at the COLUMN level across every table (one thread task per
        sampleable ``(table, col)`` pair) so the most Athena query-poll waits
        overlap, bounded by ``max_workers`` (defaults to the module cap; each
        thread holds one column across BOTH of its queries → total concurrent
        Athena queries stay within the account DML limit). Returns
        ``{table_name: {col_name: [values]}}`` for columns that pass the gate;
        best-effort — a per-column failure leaves that column unsampled and
        never aborts the others (see :meth:`_sample_one_guarded`).
        """
        if not self.enabled:
            return {}
        tasks = [(tname, col) for tname, columns in tables for col, dtype in columns if _is_sampleable(dtype)]
        if not tasks:
            return {}

        out: dict[str, dict[str, list[str]]] = {}
        workers = max_workers or ATHENA_SAMPLING_CONCURRENCY
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._sample_one_guarded, database, tname, col, max_distinct): (tname, col)
                for tname, col in tasks
            }
            for future in as_completed(futures):
                tname, col, values = future.result()
                if values:
                    out.setdefault(tname, {})[col] = values
        return out

    def _sample_one_guarded(self, database: str, table: str, col: str, max_distinct: int) -> tuple[str, str, list[str]]:
        """Never-raising wrapper around :meth:`_sample_one` for the thread pool.

        Returns ``(table, col, values)`` so results reassemble correctly no
        matter the completion order. Any Athena/LF/permission error leaves the
        column unsampled (empty list) — one failure never fails the others.
        """
        try:
            return table, col, self._sample_one(database, table, col, max_distinct)
        except Exception:
            logger.warning(
                "athena_sample_column_failed",
                extra={"database": database, "table": table, "column": col},
                exc_info=True,
            )
            return table, col, []

    def _sample_one(self, database: str, table: str, col: str, max_distinct: int) -> list[str]:
        q_table = f"{_quote_ident(database)}.{_quote_ident(table)}"
        q_col = _quote_ident(col)
        where = f"WHERE {q_col} IS NOT NULL AND CAST({q_col} AS VARCHAR) <> ''"

        # Gate query: total non-null rows + distinct count, over a capped prefix.
        # Shares probe_gate_sql with the JDBC dialects so Glue-backed sources get
        # the same bound as direct connections — the two paths ran the same
        # heuristic with separately written SQL, and only one of them was bounded.
        rows = self._run(probe_gate_sql(q_col, q_table, where, row_cap=PROBE_MAX_ROWS))
        if not rows or not rows[0]:
            return []
        total = _to_int(rows[0][0])
        distinct = _to_int(rows[0][1])
        # Enum-like: bounded distinct set with meaningful repetition. Excludes
        # unique-identifier columns (distinct ≈ total) regardless of table size.
        if not (1 <= distinct <= max_distinct and total >= MIN_REPETITION_FACTOR * distinct):
            return []

        value_rows = self._run(probe_values_sql(q_col, q_table, where, row_cap=PROBE_MAX_ROWS, limit=max_distinct))
        values = [str(r[0])[:100] for r in value_rows if r and r[0] is not None]
        # Value-shape backstop: drop low-cardinality free-text (long/multi-word).
        return values if values_look_categorical(values) else []

    def _run(self, sql: str) -> list[list]:
        """Execute an Athena query and return rows (excluding the header)."""
        athena = self._athena()
        kwargs: dict = {
            "QueryString": sql,
            "QueryExecutionContext": {"Catalog": self._catalog},
        }
        if self._workgroup:
            kwargs["WorkGroup"] = self._workgroup
        if self._output_location:
            kwargs["ResultConfiguration"] = {"OutputLocation": self._output_location}
        qid = athena.start_query_execution(**kwargs)["QueryExecutionId"]

        deadline = time.monotonic() + _QUERY_TIMEOUT_S
        while time.monotonic() < deadline:
            state = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]
            if state in _TERMINAL_STATES:
                if state != "SUCCEEDED":
                    return []
                break
            time.sleep(_POLL_INTERVAL_S)
        else:
            return []

        resp = athena.get_query_results(QueryExecutionId=qid, MaxResults=_RESULTS_PAGE_SIZE)
        rows = resp.get("ResultSet", {}).get("Rows", [])
        # Row 0 is the column header; each row is {"Data": [{"VarCharValue": ...}]}.
        out: list[list] = []
        for row in rows[1:]:
            out.append([cell.get("VarCharValue") for cell in row.get("Data", [])])
        return out


def _is_sampleable(data_type: str) -> bool:
    dt = (data_type or "").lower()
    return any(dt.startswith(p) for p in _SAMPLEABLE_TYPE_PREFIXES)


def _to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
