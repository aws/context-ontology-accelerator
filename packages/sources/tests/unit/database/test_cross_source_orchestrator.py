# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the cross-source detection orchestrator (#1088)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from coa_common.domain_models import Column, Table
from coa_sources.database.enrichment import cross_source_orchestrator as orch


def _table(name: str, ds: str, columns: list[str]) -> Table:
    return Table(
        name=name, database="db", data_source_id=ds, columns=[Column(name=c, data_type="INTEGER") for c in columns]
    )


class TestRunCrossSourceInference:
    def test_fewer_than_two_sources_is_noop(self):
        read = MagicMock()
        write = MagicMock()
        result = orch.run_cross_source_inference(
            datasource_ids=["DS#1"], read_tables=read, write_tables=write, client=MagicMock(), emitter=MagicMock()
        )
        assert result == {"sources": 1, "relationships_written": 0, "tables_updated": 0}
        read.assert_not_called()
        write.assert_not_called()

    def test_reads_all_sources_and_writes_only_changed(self, monkeypatch):
        xref = _table("account_xref", "DS#1", ["customer_id", "account_id"])
        customers = _table("customers", "DS#2", ["id", "name"])
        tables_by_ds = {"DS#1": [xref], "DS#2": [customers]}

        # Stub the (LLM) inference to return one cross-source candidate; apply runs
        # for real, so we exercise the union-read -> apply -> changed-only-write wiring.
        monkeypatch.setattr(
            orch,
            "infer_cross_source_relationships",
            lambda tables, client, emitter, **kw: [
                {
                    "source_table": "ds1.account_xref",
                    "column": "customer_id",
                    "target_table": "ds2.customers",
                    "target_column": "id",
                    "confidence": 0.9,
                    "rationale": "xref.customer_id -> customers.id",
                }
            ],
        )
        written: list[list[Table]] = []
        result = orch.run_cross_source_inference(
            datasource_ids=["DS#1", "DS#2"],
            read_tables=lambda ds: tables_by_ds[ds],
            write_tables=lambda changed: written.append(changed),
            client=MagicMock(),
            emitter=MagicMock(),
        )
        assert result["relationships_written"] == 1
        assert result["tables_updated"] == 1
        # Only the child table (which gained the FK) is written back — not customers.
        assert len(written) == 1 and written[0] == [xref]
        assert xref.foreign_keys[0].target_datasource_id == "DS#2"

    def test_no_candidates_no_write(self, monkeypatch):
        monkeypatch.setattr(orch, "infer_cross_source_relationships", lambda tables, client, emitter, **kw: [])
        write = MagicMock()
        result = orch.run_cross_source_inference(
            datasource_ids=["DS#1", "DS#2"],
            read_tables=lambda ds: [_table("t", ds, ["id"])],
            write_tables=write,
            client=MagicMock(),
            emitter=MagicMock(),
        )
        assert result["relationships_written"] == 0
        write.assert_not_called()

    def test_focus_datasource_is_threaded_to_inference(self, monkeypatch):
        # MR !1215 review: the pass must bound work to the just-enriched source,
        # so the focus id has to reach the inferrer.
        seen = {}

        def fake_infer(tables, client, emitter, **kw):
            seen.update(kw)
            return []

        monkeypatch.setattr(orch, "infer_cross_source_relationships", fake_infer)
        orch.run_cross_source_inference(
            datasource_ids=["DS#1", "DS#2"],
            read_tables=lambda ds: [_table("t", ds, ["id"])],
            write_tables=MagicMock(),
            client=MagicMock(),
            emitter=MagicMock(),
            focus_datasource_id="DS#2",
        )
        assert seen.get("focus_datasource_id") == "DS#2"


class TestEnrichedDatasourceIds:
    def test_filters_by_status_and_maps_sk_to_ds(self):
        page = SimpleNamespace(
            items=[
                {"SK": "SRC#a", "status": "APPROVED"},
                {"SK": "SRC#b", "status": "SCANNING"},  # not yet enriched -> excluded
                {"SK": "SRC#c", "status": "PENDING_REVIEW"},
            ],
            last_evaluated_key=None,
        )
        dao = MagicMock()
        dao.query.return_value = page
        ids = orch.enriched_datasource_ids("ns1", region="us-west-2", sources_table="t", dao=dao)
        assert ids == ["DS#a", "DS#c"]


class TestNamespaceLock:
    @staticmethod
    def _cond_failed():
        from botocore.exceptions import ClientError

        return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "held"}}, "UpdateItem")

    def test_acquire_true_when_conditional_write_succeeds(self):
        dao = MagicMock()
        dao.update.return_value = None  # write went through
        assert orch._acquire_namespace_lock(dao, "ns1") is True
        kw = dao.update.call_args.kwargs
        assert "attribute_not_exists(lockedAt)" in kw["condition"] and ":stale" in kw["condition_values"]

    def test_acquire_false_on_conditional_check_failure(self):
        # Another pass holds a FRESH lock -> the condition fails -> not acquired.
        dao = MagicMock()
        dao.update.side_effect = self._cond_failed()
        assert orch._acquire_namespace_lock(dao, "ns1") is False

    def test_acquire_propagates_infrastructure_errors(self):
        # A missing table / unreachable DynamoDB is NOT contention: it must raise,
        # never be mistaken for "lock held" and silently skipped.
        from botocore.exceptions import ClientError

        dao = MagicMock()
        dao.update.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}}, "UpdateItem"
        )
        with pytest.raises(ClientError):
            orch._acquire_namespace_lock(dao, "ns1")

    def test_stale_lock_is_reclaimable(self, monkeypatch):
        # A holder that crashed >TTL ago: the condition `lockedAt < :stale` must be
        # satisfied by its old timestamp. Verify the acquire passes a :stale
        # threshold that a stale lock is below (and a fresh one is not).
        fixed_now = 1_000_000
        monkeypatch.setattr(orch.time, "time", lambda: fixed_now)
        dao = MagicMock()
        dao.update.return_value = None
        assert orch._acquire_namespace_lock(dao, "ns1") is True
        stale_threshold = dao.update.call_args.kwargs["condition_values"][":stale"]
        assert stale_threshold == fixed_now - orch._LOCK_TTL_S
        # A lock written just past the TTL is reclaimable; one written just now is not.
        assert (fixed_now - orch._LOCK_TTL_S - 1) < stale_threshold
        assert not (fixed_now < stale_threshold)

    def test_release_failure_is_logged_not_raised(self):
        dao = MagicMock()
        dao.update.side_effect = RuntimeError("ddb down")
        orch._release_namespace_lock(dao, "ns1")  # must not raise

    def test_run_for_namespace_skips_when_lock_held(self, monkeypatch):
        # A pass that cannot win the lock must skip WITHOUT enumerating or reading
        # (no query, no Bedrock client build) — the holder covers the current state.
        dao = MagicMock()
        dao.update.side_effect = self._cond_failed()  # acquire fails: lock held
        monkeypatch.setattr(orch, "enriched_datasource_ids", lambda *a, **k: pytest.fail("should not enumerate"))
        result = orch.run_for_namespace(
            "ns1", "proj1", "dom1", region="us-west-2", sources_table="t", emitter=MagicMock(), dao=dao
        )
        assert result["skipped"] == "locked"
        assert result["relationships_written"] == 0
        dao.query.assert_not_called()


class TestReadWriteResilience:
    def test_one_unreadable_source_is_skipped_and_others_proceed(self, monkeypatch):
        # #1088 review: a single failing datasource read must not abort detection
        # for the remaining sources.
        good = {"DS#1": [_table("a", "DS#1", ["id"])], "DS#2": [_table("b", "DS#2", ["id"])]}

        def read(ds):
            if ds == "DS#3":
                raise RuntimeError("datazone 500")
            return good[ds]

        monkeypatch.setattr(orch, "infer_cross_source_relationships", lambda t, c, e, **kw: [])
        emitter = MagicMock()
        result = orch.run_cross_source_inference(
            datasource_ids=["DS#1", "DS#2", "DS#3"],
            read_tables=read,
            write_tables=MagicMock(),
            client=MagicMock(),
            emitter=emitter,
        )
        assert result["sources"] == 2  # the two readable ones
        emitted = {c.args[0] for c in emitter.emit_metric.call_args_list}
        assert "CrossSourceReadFailed" in emitted

    def test_write_failure_is_reported_not_raised(self, monkeypatch):
        # A failed write must not raise (relationships are re-derived by the next
        # idempotent pass) but must be observable via a metric + result flag.
        xref = _table("xref", "DS#1", ["customer_id"])
        cust = _table("customers", "DS#2", ["id"])
        monkeypatch.setattr(
            orch,
            "infer_cross_source_relationships",
            lambda t, c, e, **kw: [
                {
                    "source_table": "ds1.xref",
                    "column": "customer_id",
                    "target_table": "ds2.customers",
                    "target_column": "id",
                }
            ],
        )
        emitter = MagicMock()

        def write(_changed):
            raise RuntimeError("datazone write failed")

        result = orch.run_cross_source_inference(
            datasource_ids=["DS#1", "DS#2"],
            read_tables=lambda ds: {"DS#1": [xref], "DS#2": [cust]}[ds],
            write_tables=write,
            client=MagicMock(),
            emitter=emitter,
        )
        assert result.get("write_failed") is True
        assert result["relationships_written"] == 0
        emitted = {c.args[0] for c in emitter.emit_metric.call_args_list}
        assert "CrossSourceWriteFailed" in emitted
