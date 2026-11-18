# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""KG-build progress metrics via the GraphRAG toolkit ``ProgressMonitor``.

The toolkit (>= 3.18.6) exposes a native ``ProgressMonitor`` construct: the
extract/build pipeline calls ``increment_*`` methods at document boundaries,
**single-threaded from the main process** (per the toolkit's contract — no
thread/process fan-out reaches the monitor). We implement it to write the
running counts straight into the shared per-source DynamoDB record so the
Source Detail UI can poll live progress.

``ProgressMonitor`` is invoked only from the main process, so the counts are
plain additive Numbers (atomic ``ADD``).

Metrics written (Numbers, keyed under PK=METRICS#{ns}, SK=KGBUILD#{ds}):

  documents_processed  — distinct source docs that completed LLM extraction
  chunks_llm           — chunks that completed LLM extraction (~ prompts/2)
  chunks_graph         — chunk nodes written to the graph store (Neptune)
  chunks_embed         — chunk nodes written to the vector store (OSS)

Each ``increment_*`` call issues one atomic ``ADD`` to the record. Best-effort:
a DynamoDB error is logged and swallowed so instrumentation never fails the
build. Disable with ``KG_METRICS_ENABLED=false``.

This module also provides :func:`start_heartbeat`, a daemon thread that logs a
periodic liveness line (elapsed, RSS, in-process counter deltas). The toolkit
reports progress only at document-batch boundaries, which can be tens of minutes
apart, so without a heartbeat a task that stalls mid-batch is indistinguishable
from one that is working — the log simply goes silent until the task dies.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import structlog

logger = structlog.get_logger()

METRICS_ENABLED = os.environ.get("KG_METRICS_ENABLED", "true").lower() == "true"

# Seconds between heartbeat lines. 0 disables the heartbeat.
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("KG_HEARTBEAT_INTERVAL_SECONDS", "60"))

# Field names on the metrics record. The Source Detail API (sources_handler.py)
# reads these to surface documentsProcessed / chunksLLM / chunksEmbed / chunksGraph.
_F_DOCUMENTS = "documents_processed"
_F_CHUNKS_LLM = "chunks_llm"
_F_CHUNKS_GRAPH = "chunks_graph"
_F_CHUNKS_EMBED = "chunks_embed"


def _metrics_key(namespace_id: str, doc_source_id: str) -> dict[str, str]:
    """DynamoDB key for the dedicated per-source metrics record.

    A dedicated ``METRICS#`` partition keeps these writes off the source-facing
    item, so live progress polling never contends with the source row.
    """
    return {"PK": f"METRICS#{namespace_id}", "SK": f"KGBUILD#{doc_source_id}"}


# Running in-process mirror of the counts sent to DynamoDB, so the heartbeat can
# report progress without spending a DDB read per beat. Written by the progress
# monitor (main process, single-threaded) and read by the heartbeat thread, hence
# the lock.
_counters: dict[str, int] = {}
_counters_lock = threading.Lock()


def _record_local(field: str, count: int) -> None:
    """Mirror an increment into the in-process counter snapshot."""
    with _counters_lock:
        _counters[field] = _counters.get(field, 0) + count


def counter_snapshot() -> dict[str, int]:
    """Return a copy of the in-process counts recorded so far this run."""
    with _counters_lock:
        return dict(_counters)


def _rss_mb() -> float | None:
    """Resident set size of this process in MiB, or None if unavailable.

    Read from ``/proc/self/status`` rather than via ``psutil`` to avoid adding a
    dependency. Returns None on platforms without procfs (e.g. macOS dev boxes);
    the container runs Linux, which is where this signal matters.
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def start_heartbeat(*, namespace_id: str, doc_source_id: str) -> threading.Event:
    """Start a daemon thread logging periodic liveness until the returned Event is set.

    The toolkit reports progress only at document-batch boundaries, which can be
    15-25 minutes apart. Each beat logs elapsed time, RSS, and the counter deltas
    since the previous beat, so "stalled" can be told from "slow".

    Returns the stop Event. Setting it ends the thread; since the thread is a
    daemon, forgetting to stop it will not block process exit.
    """
    stop = threading.Event()
    if HEARTBEAT_INTERVAL_SECONDS <= 0:
        return stop

    started = time.time()

    def _beat() -> None:
        previous: dict[str, int] = {}
        while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            current = counter_snapshot()
            # Emitted even when nothing changed: a beat with all-zero deltas is
            # precisely the signal that the task is alive but making no progress.
            delta = {f"{k}_delta": current[k] - previous.get(k, 0) for k in current}
            previous = current
            logger.info(
                "kg_build heartbeat",
                elapsed_seconds=round(time.time() - started, 1),
                rss_mb=_rss_mb(),
                namespace_id=namespace_id,
                doc_source_id=doc_source_id,
                **current,
                **delta,
            )

    threading.Thread(target=_beat, name="kg-build-heartbeat", daemon=True).start()
    logger.info("kg_metrics: heartbeat started", interval_seconds=HEARTBEAT_INTERVAL_SECONDS)
    return stop


class NoOpProgressMonitor:
    """No-op progress monitor used when metrics are off or the toolkit is absent.

    Every increment method does nothing. Kept as a plain class (not a toolkit
    subclass) so importing this module never requires graphrag_toolkit, letting
    callers pass it unconditionally in unit stubs or when metrics are disabled.
    """

    def increment_llm_processed_documents(self, count: int = 1) -> None:
        """No-op: discard the LLM-extraction document count."""

    def increment_llm_processed_chunks(self, count: int = 1) -> None:
        """No-op: discard the LLM-extraction chunk count."""

    def increment_graph_processed_documents(self, count: int = 1) -> None:
        """No-op: discard the graph-construction document count."""

    def increment_graph_processed_chunks(self, count: int = 1) -> None:
        """No-op: discard the graph-construction chunk count."""

    def increment_vector_processed_documents(self, count: int = 1) -> None:
        """No-op: discard the vector-indexing document count."""

    def increment_vector_processed_chunks(self, count: int = 1) -> None:
        """No-op: discard the vector-indexing chunk count."""


def make_progress_monitor(*, table: str, region: str, namespace_id: str, doc_source_id: str):
    """Build a ``ProgressMonitor`` that writes counts to DynamoDB on each event.

    Returns a :class:`NoOpProgressMonitor` when metrics are disabled or the
    toolkit's ``ProgressMonitor`` base class can't be imported (unit tests /
    stubbed environments), so callers can always pass the result to
    ``extract``/``build``/``extract_and_build`` unconditionally.
    """
    if not METRICS_ENABLED:
        return NoOpProgressMonitor()

    try:
        from graphrag_toolkit.lexical_graph.indexing.progress_monitor import ProgressMonitor
    except (ImportError, AttributeError):
        logger.debug("kg_metrics: graphrag ProgressMonitor not importable — using no-op monitor")
        return NoOpProgressMonitor()

    from coa_common.dao import DynamoDBDAO

    key = _metrics_key(namespace_id, doc_source_id)

    class _DdbProgressMonitor(ProgressMonitor):
        """Writes running KG-build counts to DDB on every increment.

        The toolkit invokes these single-threaded from the main process, so a
        plain atomic ``ADD`` per call is exact — no cross-process reconciliation
        needed. Each write is best-effort; a DDB error is logged and swallowed.
        """

        def __init__(self) -> None:
            self._dao = DynamoDBDAO(table, region=region)

        def _add(self, field: str, count: int) -> None:
            if count <= 0:
                return
            _record_local(field, int(count))
            try:
                self._dao.atomic_add(key, {field: int(count)})
                logger.debug("kg_metrics increment", field=field, count=count, pk=key["PK"], sk=key["SK"])
            except Exception as e:  # noqa: BLE001 — instrumentation must never fail the build
                logger.debug("kg_metrics increment failed (non-fatal)", field=field, error=repr(e))

        # LLM extraction stage.
        def increment_llm_processed_documents(self, count: int = 1) -> None:
            # ``documents_processed`` is anchored to LLM-extraction completion —
            # the first stage a document clears — matching the prior semantics.
            self._add(_F_DOCUMENTS, count)

        def increment_llm_processed_chunks(self, count: int = 1) -> None:
            self._add(_F_CHUNKS_LLM, count)

        # Graph-store (Neptune) build stage.
        def increment_graph_processed_documents(self, count: int = 1) -> None:
            # Document-level graph progress is not separately surfaced today;
            # chunk-level below is the tracked signal.
            pass

        def increment_graph_processed_chunks(self, count: int = 1) -> None:
            self._add(_F_CHUNKS_GRAPH, count)

        # Vector-store (OSS) build stage.
        def increment_vector_processed_documents(self, count: int = 1) -> None:
            pass

        def increment_vector_processed_chunks(self, count: int = 1) -> None:
            self._add(_F_CHUNKS_EMBED, count)

    monitor = _DdbProgressMonitor()
    logger.info(
        "kg_metrics: DDB ProgressMonitor active",
        table=table,
        namespace_id=namespace_id,
        doc_source_id=doc_source_id,
    )
    return monitor


def write_record_metadata(
    *, table: str, region: str, namespace_id: str, doc_source_id: str, extra: dict[str, Any] | None = None
) -> None:
    """Stamp the metrics record's identifying metadata and run-level scalars.

    Writes the record header (recordType/ids) plus any ``extra`` fields (e.g.
    total run time). Best-effort SET; never raises. The counters themselves
    arrive via ``ProgressMonitor`` atomic ADDs; this is just the header + any
    run-level scalars written at the end.
    """
    if not METRICS_ENABLED:
        return
    try:
        from coa_common.dao import DynamoDBDAO

        dao = DynamoDBDAO(table, region=region)
        fields: dict[str, Any] = {
            "recordType": "KG_BUILD_METRICS",
            "namespaceId": namespace_id,
            "docSourceId": doc_source_id,
        }
        if extra:
            fields.update(_floats_to_decimal(extra))
        dao.update(_metrics_key(namespace_id, doc_source_id), fields, auto_timestamp=False)
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.debug("kg_metrics: metadata write failed (non-fatal)", error=repr(e))


def reset_metrics(*, table: str, region: str, namespace_id: str, doc_source_id: str) -> None:
    """Delete any prior metrics record before a (re)build.

    The per-stage counters are accumulated via atomic ``ADD``, so a record left
    over from an earlier build would make this run's counts add on top of the
    previous totals (a rescan would report ~2x the true chunk/document counts).
    Clearing the record first makes each build report its own totals. Best-effort
    SET/DELETE; never raises — a failure here must not fail the build.
    """
    if not METRICS_ENABLED:
        return
    try:
        from coa_common.dao import DynamoDBDAO

        dao = DynamoDBDAO(table, region=region)
        dao.delete(_metrics_key(namespace_id, doc_source_id))
    except Exception as e:  # noqa: BLE001 — best-effort
        logger.debug("kg_metrics: reset failed (non-fatal)", error=repr(e))


def _floats_to_decimal(obj):
    """Recursively convert floats to Decimal for DynamoDB (rejects native floats).

    NaN/Inf become None. Mirrors the shared helper used elsewhere in the repo.
    """
    import math
    from decimal import Decimal

    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_floats_to_decimal(v) for v in obj]
    return obj
