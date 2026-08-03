# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ``source_type`` filter on :mod:`dynamo_store`.

Validates the schema delta added by Task 2.1 of the unstructured-ontology-
induction-api spec:

- ``create_proposal`` and ``put_job`` accept a ``source_type`` parameter
  defaulting to ``"STRUCTURED"``.
- ``list_proposals`` and ``list_jobs`` accept an optional ``source_type``
  filter with asymmetric backfill semantics:

    * ``source_type="STRUCTURED"`` matches both items with the attribute
      set to ``"STRUCTURED"`` AND items that lack the attribute entirely
      (pre-schema-delta data).
    * ``source_type="UNSTRUCTURED"`` matches only items with the
      attribute set to ``"UNSTRUCTURED"`` exactly. Pre-delta items are
      never returned for this filter.

All tests use an in-memory fake Dynamo table — no AWS traffic.

Requirements: 3.3, 3.7
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from coa_common.constants import RESOURCE_PREFIX

pytestmark = pytest.mark.unit


# ── Fake Dynamo table with FilterExpression support ────────────────────


class _FakeDynamoTable:
    """In-memory stand-in that mimics the boto3 Dynamo Table interface.

    Implements only the operations exercised by ``dynamo_store``'s job +
    proposal helpers: ``put_item``, ``get_item``, ``update_item`` (SET
    only), and ``scan`` with FilterExpression evaluation rich enough to
    cover the asymmetric ``source_type`` backfill rule.

    The filter evaluator handles the exact shapes ``list_proposals`` and
    ``list_jobs`` produce:

      - ``begins_with(PK, :pfx)``
      - ``SK = :sk``
      - ``#alias = :v`` with ``ExpressionAttributeNames``
      - ``attr = :v``
      - ``(source_type = :src OR attribute_not_exists(source_type))``
      - clauses joined by ``" AND "``

    Anything else raises so tests fail loudly rather than silently
    returning wrong results.
    """

    def __init__(self, page_size: int | None = None) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        # Intrinsic scan page size. Real DynamoDB paginates a scan on its own
        # (≤1 MB per page) even with no ``Limit`` — set this to force multi-page
        # drains (e.g. an orphan on page 2). ``None`` = everything in one page.
        self.page_size = page_size

    def put_item(self, Item):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)
        return {}

    def delete_item(self, Key, **kwargs):
        # Every prod ``delete_item`` call is unconditional (release_induction_lock,
        # delete_proposal, the registry delete, and the batch-writer wipe) — none
        # passes a ConditionExpression, so the fake models only the plain delete.
        self.items.pop((Key["PK"], Key["SK"]), None)
        return {}

    def batch_writer(self):
        # Minimal context-manager mirroring boto3 Table.batch_writer — used by
        # delete_all_namespace_items. Writes flush immediately to the in-memory
        # store (no real batching needed for tests).
        table = self

        class _Batch:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def delete_item(self_inner, Key):
                table.delete_item(Key=Key)

        return _Batch()

    def get_item(self, Key):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def update_item(self, Key, UpdateExpression, **kwargs):
        item = dict(self.items.get((Key["PK"], Key["SK"]), {**Key}))
        values = kwargs.get("ExpressionAttributeValues", {})
        names = kwargs.get("ExpressionAttributeNames", {}) or {}
        expr = UpdateExpression.strip()
        assert expr.startswith("SET "), f"FakeDynamoTable only supports SET, got: {expr!r}"
        body = expr[4:]
        for part in (p.strip() for p in body.split(",") if p.strip()):
            lhs, rhs = (s.strip() for s in part.split("=", 1))
            for alias, real in names.items():
                lhs = lhs.replace(alias, real)
            item[lhs] = values.get(rhs, rhs)
        self.items[(Key["PK"], Key["SK"])] = item
        return {}

    def scan(self, **kwargs):
        filter_expr: str = kwargs.get("FilterExpression", "")
        values: dict[str, Any] = kwargs.get("ExpressionAttributeValues", {}) or {}
        names: dict[str, str] = kwargs.get("ExpressionAttributeNames", {}) or {}
        # DynamoDB ``Limit`` caps items SCANNED per page (not returned), so a page
        # can return fewer matches than requested and set LastEvaluatedKey. We
        # model that faithfully: walk the raw store from ExclusiveStartKey, read
        # at most ``limit`` items (pre-filter), apply the FilterExpression to that
        # slice, and emit LastEvaluatedKey when more raw items remain. This is
        # what makes a paginated drain testable with a row on page 2. Without a
        # Limit, everything is one page.
        ordered = list(self.items.items())  # [((PK, SK), item), ...] insertion order
        start = 0
        start_key = kwargs.get("ExclusiveStartKey")
        if start_key is not None:
            want = (start_key["PK"], start_key["SK"])
            start = next((i + 1 for i, (k, _) in enumerate(ordered) if k == want), len(ordered))
        # An explicit ``Limit`` wins; otherwise fall back to the table's
        # intrinsic page size (models DynamoDB's own ≤1 MB scan paging, which
        # happens even with no Limit — this is what makes a drain loop testable
        # with a row on page 2).
        limit: int | None = kwargs.get("Limit", self.page_size)
        page = ordered[start:] if limit is None else ordered[start : start + limit]

        out: list[dict] = []
        for _key, item in page:
            if filter_expr and not _evaluate_filter(filter_expr, item, values, names):
                continue
            out.append(dict(item))
        resp: dict = {"Items": out}
        # More raw items past this page → hand back the last key we read so the
        # caller's LastEvaluatedKey loop fetches the next page.
        last_read = start + len(page)
        if limit is not None and last_read < len(ordered):
            last_pk, last_sk = ordered[last_read - 1][0]
            resp["LastEvaluatedKey"] = {"PK": last_pk, "SK": last_sk}
        return resp


class _FakeDao:
    """Adapts the DynamoDBDAO get/put/update surface onto _FakeDynamoTable.

    The ingest-job helpers route through ``dynamo_store._get_dao()`` (a real
    DynamoDBDAO in prod); in tests we back it with the same in-memory table so
    the helpers stay hermetic. Only the methods the helpers call are needed.
    """

    def __init__(self, table: _FakeDynamoTable) -> None:
        self._table = table

    def put(self, item, *, condition=None, auto_timestamp=True):
        self._table.put_item(Item=dict(item))

    def get(self, key, *, projection=None):
        return self._table.get_item(Key=key).get("Item")

    def update(self, key, update_fields, *, auto_timestamp=True, **kwargs):
        # Mirror DynamoDBDAO: alias every field name (#a0, #a1, …) so reserved
        # words like ``status``/``error`` are legal — exactly the behavior the
        # helpers now rely on instead of hand-rolling aliases.
        names = {}
        values = {}
        parts = []
        for i, (field, value) in enumerate(update_fields.items()):
            alias = f"#a{i}"
            placeholder = f":v{i}"
            names[alias] = field
            values[placeholder] = value
            parts.append(f"{alias} = {placeholder}")
        self._table.update_item(
            Key=key,
            UpdateExpression="SET " + ", ".join(parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return True


# ── Fake S3 client for the turtle offload ──────────────────────────────
#
# Mainline reworked ``create_proposal`` so the ontology / R2RML Turtle is
# offloaded to S3 (``_get_s3().put_object`` + ``copy_object``) and only the
# S3 keys are persisted on the DynamoDB item; ``get_proposal_by_id``
# hydrates the Turtle back via ``get_object``. Unit tests that exercise
# ``create_proposal`` directly must therefore stub the S3 client too,
# otherwise the call reaches real S3 and fails with ``AccessDenied``.


class _FakeS3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeNoSuchKey(Exception):
    pass


class _FakeS3Exceptions:
    NoSuchKey = _FakeNoSuchKey


class _FakeS3Client:
    """In-memory stand-in for the boto3 S3 client used by ``dynamo_store``.

    Implements only ``put_object``, ``copy_object`` and ``get_object`` —
    the surface ``create_proposal`` / ``get_proposal_by_id`` exercise.
    Objects are keyed by ``(bucket, key)``; ``get_object`` raises the
    boto3-shaped ``NoSuchKey`` for a missing key so the store's
    ``except _get_s3().exceptions.NoSuchKey`` path works.
    """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.exceptions = _FakeS3Exceptions()

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else str(Body).encode("utf-8")
        return {}

    def copy_object(self, Bucket, Key, CopySource, **kwargs):
        src = (CopySource["Bucket"], CopySource["Key"])
        self.objects[(Bucket, Key)] = self.objects.get(src, b"")
        return {}

    def get_object(self, Bucket, Key, **kwargs):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey()
        return {"Body": _FakeS3Body(self.objects[(Bucket, Key)])}

    def get_paginator(self, operation_name):
        assert operation_name == "list_objects_v2"
        objects = self.objects

        class _Paginator:
            def paginate(self_inner, Bucket, Prefix, **kwargs):
                contents = [{"Key": key} for (bucket, key) in objects if bucket == Bucket and key.startswith(Prefix)]
                yield {"Contents": contents} if contents else {}

        return _Paginator()

    def delete_objects(self, Bucket, Delete, **kwargs):
        for obj in Delete.get("Objects", []):
            self.objects.pop((Bucket, obj["Key"]), None)
        return {}


# Top-level OR is unparenthesized in the FilterExpression; only the
# ``source_type`` clause uses inner parentheses. We split on ``" AND "``
# at the top level (none of the AND-clauses contain " AND " inside their
# parentheses, so a plain string split is safe for the shapes
# dynamo_store produces).
_FILTER_AND_RE = re.compile(r"\s+AND\s+")


def _evaluate_filter(
    filter_expr: str,
    item: dict,
    values: dict[str, Any],
    names: dict[str, str],
) -> bool:
    """Evaluate a (limited) DynamoDB FilterExpression against ``item``."""
    for clause in _FILTER_AND_RE.split(filter_expr.strip()):
        if not _evaluate_clause(clause.strip(), item, values, names):
            return False
    return True


def _evaluate_clause(
    clause: str,
    item: dict,
    values: dict[str, Any],
    names: dict[str, str],
) -> bool:
    # Strip a single layer of surrounding parentheses if present.
    if clause.startswith("(") and clause.endswith(")"):
        inner = clause[1:-1].strip()
        # Top-level OR support — only used for the source_type backfill
        # clause, which has the shape:
        #   "(source_type = :src OR attribute_not_exists(source_type))"
        if " OR " in inner and "attribute_not_exists" in inner:
            return any(_evaluate_clause(sub.strip(), item, values, names) for sub in inner.split(" OR "))
        return _evaluate_clause(inner, item, values, names)

    # begins_with(PK, :pfx)
    m = re.fullmatch(r"begins_with\(\s*(\w+)\s*,\s*(:\w+)\s*\)", clause)
    if m:
        attr, ref = m.group(1), m.group(2)
        return str(item.get(attr, "")).startswith(values.get(ref, ""))

    # contains(PK, :marker) — the cross-namespace proposal scan marker
    # (``list_proposals(namespace=None)`` filters on ``contains(PK, "#PROPOSAL#")``).
    m = re.fullmatch(r"contains\(\s*(\w+)\s*,\s*(:\w+)\s*\)", clause)
    if m:
        attr, ref = m.group(1), m.group(2)
        return values.get(ref, "") in str(item.get(attr, ""))

    # attribute_not_exists(attr)
    m = re.fullmatch(r"attribute_not_exists\(\s*(\w+)\s*\)", clause)
    if m:
        return m.group(1) not in item

    # attr = :v   (with optional ``#alias = :v`` form)
    m = re.fullmatch(r"(#?\w+)\s*=\s*(:\w+)", clause)
    if m:
        lhs, rhs = m.group(1), m.group(2)
        attr = names.get(lhs, lhs)
        return item.get(attr) == values.get(rhs)

    raise AssertionError(f"FakeDynamoTable cannot evaluate clause: {clause!r}")


@pytest.fixture()
def fake_dynamo(monkeypatch):
    """Patch ``dynamo_store._get_table`` to return a fresh fake table.

    Each test gets its own table instance, so tests are hermetic. The S3
    client is stubbed too: mainline offloads proposal Turtle to S3, so
    ``create_proposal`` / ``get_proposal_by_id`` touch S3 even in unit
    tests. Without the stub those calls reach real S3 and fail with
    ``AccessDenied``.
    """
    table = _FakeDynamoTable()
    s3 = _FakeS3Client()
    # Reset any cached real boto3 clients from prior test runs.
    import coa_ontology.dynamo_store as ds

    ds._table = None
    ds._s3 = None
    ds._dao = None
    monkeypatch.setattr(
        "coa_ontology.dynamo_store._get_table",
        lambda: table,
    )
    monkeypatch.setattr(
        "coa_ontology.dynamo_store._get_s3",
        lambda: s3,
    )
    # The ingest-job helpers go through DynamoDBDAO; back it with the same fake
    # table so those tests stay hermetic (no AWS).
    monkeypatch.setattr(
        "coa_ontology.dynamo_store._get_dao",
        lambda: _FakeDao(table),
    )
    return table


# ── Helpers ─────────────────────────────────────────────────────────────


def _seed_pre_delta_proposal(table: _FakeDynamoTable, *, namespace: str, proposal_id: str):
    """Write a proposal item that lacks ``source_type`` entirely.

    Bypasses ``create_proposal`` — its default would set the attribute to
    ``"STRUCTURED"``, defeating the pre-delta simulation.
    """
    table.put_item(
        Item={
            "PK": f"{namespace}#PROPOSAL#{proposal_id}",
            "SK": "META",
            "proposal_id": proposal_id,
            "job_id": f"job-{proposal_id}",
            "namespace": namespace,
            "ontology_id": "https://example.com/onto",
            "proposal_type": "induction",
            "status": "pending",
            "ontology_turtle": "",
            "r2rml_turtle": "",
        }
    )


def _seed_pre_delta_job(table: _FakeDynamoTable, *, namespace: str, job_id: str):
    """Write a job item that lacks ``source_type`` entirely."""
    table.put_item(
        Item={
            "PK": f"{namespace}#JOB#{job_id}",
            "SK": "STATUS",
            "job_id": job_id,
            "namespace": namespace,
            "status": "pending",
            "ontology_uri_prefix": "https://example.com/onto",
            "label": "",
            "datasource_ids": [],
            "datasources": [],
            "confidence_threshold": "0.80",
            "scoring_strategy": "lexical",
        }
    )


# ── Proposal matches S3 offload (grounding-override 400 KB regression) ──


class TestResolveOntologyBucket:
    """The bucket resolver must never fall back to a hardcoded foreign account."""

    def test_env_var_wins(self, monkeypatch):
        from coa_ontology import dynamo_store as ds

        monkeypatch.setenv("ONTOLOGY_ARTIFACTS_BUCKET", "my-bucket")
        assert ds.resolve_ontology_bucket() == "my-bucket"

    def test_derives_from_current_account_when_unset(self, monkeypatch):
        """No env var → derive ``{prefix}-{env}-ontology-artifacts-{account}``
        from the CURRENT account via STS, not a hardcoded CI-account default."""
        from coa_ontology import dynamo_store as ds

        monkeypatch.delenv("ONTOLOGY_ARTIFACTS_BUCKET", raising=False)
        monkeypatch.setenv("SCL_ENVIRONMENT", "dev")

        class _FakeSts:
            def get_caller_identity(self):
                return {"Account": "111122223333"}

        monkeypatch.setattr(ds.boto3, "client", lambda *a, **k: _FakeSts())
        assert ds.resolve_ontology_bucket() == f"{RESOURCE_PREFIX}-dev-ontology-artifacts-111122223333"

    def test_prefix_already_carrying_env_is_not_doubled(self, monkeypatch):
        """CDK injects RESOURCE_PREFIX into deployed compute as ``{prefix}-{env}-``.

        The derived name must be ``scl-dev-ontology-artifacts-…``, NOT
        ``scl-dev-dev-…``. Regression guard for the bucket name being built from
        a brand literal instead of the deployment prefix, which resolved a bucket
        that exists in no environment (NoSuchBucket for every caller outside the
        deployed compute, e.g. integ tests).
        """
        import importlib

        monkeypatch.setenv("RESOURCE_PREFIX", "scl-dev-")
        monkeypatch.delenv("ONTOLOGY_ARTIFACTS_BUCKET", raising=False)
        monkeypatch.setenv("SCL_ENVIRONMENT", "dev")

        import coa_common.constants as consts

        importlib.reload(consts)
        from coa_ontology import dynamo_store as ds

        ds = importlib.reload(ds)
        try:

            class _FakeSts:
                def get_caller_identity(self):
                    return {"Account": "111122223333"}

            monkeypatch.setattr(ds.boto3, "client", lambda *a, **k: _FakeSts())
            assert ds.resolve_ontology_bucket() == "scl-dev-ontology-artifacts-111122223333"
        finally:
            monkeypatch.undo()
            importlib.reload(consts)
            importlib.reload(ds)

    def test_returns_empty_when_account_lookup_fails(self, monkeypatch):
        """STS failure → "" so the first S3 call fails loudly, never touching a
        wrong-account bucket."""
        from coa_ontology import dynamo_store as ds

        monkeypatch.delenv("ONTOLOGY_ARTIFACTS_BUCKET", raising=False)

        def _boom(*a, **k):
            raise RuntimeError("no creds")

        monkeypatch.setattr(ds.boto3, "client", _boom)
        assert ds.resolve_ontology_bucket() == ""


class TestProposalMatchesS3Offload:
    """Grounding overrides must offload ``matches`` to S3, never write inline.

    Regression for the ``POST /proposals/update`` 500: ``_apply_grounding_
    overrides`` rebuilt ``metadata={**meta, "matches": ...}`` and wrote it
    inline, tripping DynamoDB's 400 KB item limit (UpdateItem
    ``ValidationException: Item size has exceeded the maximum allowed size``).
    The matches now go to S3 with only the key persisted on the item.
    """

    def test_put_proposal_matches_s3_writes_to_s3_and_returns_key(self, fake_dynamo, monkeypatch):
        from coa_ontology import dynamo_store as ds

        captured: dict = {}
        real_put = ds._get_s3().put_object

        def _spy(**kwargs):
            captured.update(kwargs)
            return real_put(**kwargs)

        monkeypatch.setattr(ds._get_s3(), "put_object", _spy)

        key = ds.put_proposal_matches_s3("p-1", [{"source_table": "t", "match_type": "novel"}], namespace="ns")

        assert key == "proposals/ns/p-1/latest/matches.json"
        assert captured["Key"] == key
        assert b"source_table" in captured["Body"]

    def test_serializes_decimal_values(self, fake_dynamo):
        """Matches carrying DynamoDB ``Decimal`` (e.g. round-tripped through a
        read) must still JSON-encode — json.dumps rejects Decimal by default,
        which 500'd the grounding-override write before the ``default=`` hook."""
        from decimal import Decimal

        from coa_ontology import dynamo_store as ds

        # similarity as Decimal (whole + fractional) must not raise.
        matches = [{"source_table": "t", "similarity": Decimal("0.85"), "rank": Decimal("3")}]
        key = ds.put_proposal_matches_s3("p-dec", matches, namespace="ns")
        raw = ds._get_s3().get_object(Bucket=ds._S3_BUCKET, Key=key)["Body"].read().decode("utf-8")
        assert '"similarity": 0.85' in raw
        assert '"rank": 3' in raw

    def test_grounding_override_write_keeps_item_small(self, fake_dynamo):
        """Simulate the override write: big matches to S3, only the key inline."""
        from coa_ontology import dynamo_store as ds

        ns, pid = "ns-big", "p-big"
        # A matches blob large enough to blow the 400 KB inline limit.
        big_matches = [{"source_table": f"t{i}", "candidates": ["x" * 500 for _ in range(20)]} for i in range(60)]
        matches_key = ds.put_proposal_matches_s3(pid, big_matches, namespace=ns)
        ds.update_proposal(
            pid,
            namespace=ns,
            ontology_s3_key="proposals/ns-big/p-big/latest/ontology.ttl",
            matches_s3_key=matches_key,
            status="updated",
            metadata={"datasource_ids": ["d1"]},  # small, no matches/constraint_config
        )

        raw = fake_dynamo.items[(f"{ns}#PROPOSAL#{pid}", "META")]
        # The heavy blob must NOT be inline on the item.
        assert "matches" not in (raw.get("metadata") or {})
        assert raw["matches_s3_key"] == matches_key
        assert raw["status"] == "updated"


# ── create_proposal: source_type default + persistence ─────────────────


class TestCreateProposalSourceType:
    def test_default_source_type_is_structured(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.create_proposal(
            proposal_id="p-default",
            job_id="j-default",
            namespace="ns-default",
            ontology_id="https://example.com/onto",
            proposal_type="induction",
            ontology_turtle="",
            r2rml_turtle="",
        )

        item = ds.get_proposal_by_id("p-default", namespace="ns-default")
        assert item is not None
        assert item["source_type"] == "STRUCTURED"

    def test_explicit_unstructured_is_persisted(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.create_proposal(
            proposal_id="p-unstr",
            job_id="j-unstr",
            namespace="ns-unstr",
            ontology_id="https://example.com/onto",
            proposal_type="induction",
            ontology_turtle="",
            r2rml_turtle="",
            source_type="UNSTRUCTURED",
        )

        item = ds.get_proposal_by_id("p-unstr", namespace="ns-unstr")
        assert item is not None
        assert item["source_type"] == "UNSTRUCTURED"

    def test_explicit_structured_is_persisted(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.create_proposal(
            proposal_id="p-str",
            job_id="j-str",
            namespace="ns-str",
            ontology_id="https://example.com/onto",
            proposal_type="induction",
            ontology_turtle="",
            r2rml_turtle="",
            source_type="STRUCTURED",
        )

        item = ds.get_proposal_by_id("p-str", namespace="ns-str")
        assert item is not None
        assert item["source_type"] == "STRUCTURED"


# ── list_proposals: source_type filter semantics ───────────────────────


class TestListProposalsSourceTypeFilter:
    """Asymmetric backfill: STRUCTURED includes pre-delta items; UNSTRUCTURED does not."""

    @pytest.fixture()
    def seeded(self, fake_dynamo):
        """Three proposals in one namespace: structured, unstructured, pre-delta."""
        from coa_ontology import dynamo_store as ds

        ns = "ns-list"
        ds.create_proposal(
            proposal_id="p-structured",
            job_id="j-structured",
            namespace=ns,
            ontology_id="https://example.com/onto",
            proposal_type="induction",
            ontology_turtle="",
            r2rml_turtle="",
            source_type="STRUCTURED",
        )
        ds.create_proposal(
            proposal_id="p-unstructured",
            job_id="j-unstructured",
            namespace=ns,
            ontology_id="https://example.com/onto",
            proposal_type="induction",
            ontology_turtle="",
            r2rml_turtle="",
            source_type="UNSTRUCTURED",
        )
        _seed_pre_delta_proposal(fake_dynamo, namespace=ns, proposal_id="p-predelta")
        return ns

    def test_no_filter_returns_all_three(self, seeded):
        from coa_ontology import dynamo_store as ds

        rows = ds.list_proposals(namespace=seeded)
        ids = {r["proposal_id"] for r in rows}
        assert ids == {"p-structured", "p-unstructured", "p-predelta"}

    def test_limit_caps_results(self, seeded):
        """An explicit small limit caps the number of items returned."""
        from coa_ontology import dynamo_store as ds

        rows = ds.list_proposals(namespace=seeded, limit=2)
        assert len(rows) == 2

    def test_limit_none_drains_all(self, seeded):
        """limit=None returns every matching proposal (no cap) — used by the
        grounded-proposal cleanup so a large namespace can't silently truncate."""
        from coa_ontology import dynamo_store as ds

        rows = ds.list_proposals(namespace=seeded, limit=None)
        ids = {r["proposal_id"] for r in rows}
        assert ids == {"p-structured", "p-unstructured", "p-predelta"}

    def test_structured_filter_includes_predelta(self, seeded):
        """Pre-delta items lack the attribute and are matched as STRUCTURED."""
        from coa_ontology import dynamo_store as ds

        rows = ds.list_proposals(namespace=seeded, source_type="STRUCTURED")
        ids = {r["proposal_id"] for r in rows}
        assert ids == {"p-structured", "p-predelta"}
        # The unstructured one is NOT included.
        assert "p-unstructured" not in ids

    def test_unstructured_filter_excludes_predelta(self, seeded):
        """The UNSTRUCTURED filter never matches items missing the attribute."""
        from coa_ontology import dynamo_store as ds

        rows = ds.list_proposals(namespace=seeded, source_type="UNSTRUCTURED")
        ids = {r["proposal_id"] for r in rows}
        assert ids == {"p-unstructured"}

    def test_unstructured_filter_returns_empty_when_only_predelta_exists(self, fake_dynamo):
        """A namespace with only pre-delta items returns nothing for UNSTRUCTURED."""
        from coa_ontology import dynamo_store as ds

        _seed_pre_delta_proposal(fake_dynamo, namespace="ns-only-predelta", proposal_id="p1")
        _seed_pre_delta_proposal(fake_dynamo, namespace="ns-only-predelta", proposal_id="p2")

        rows = ds.list_proposals(namespace="ns-only-predelta", source_type="UNSTRUCTURED")
        assert rows == []

    def test_structured_filter_returns_predelta_when_only_predelta_exists(self, fake_dynamo):
        """A namespace with only pre-delta items returns them all for STRUCTURED."""
        from coa_ontology import dynamo_store as ds

        _seed_pre_delta_proposal(fake_dynamo, namespace="ns-only-predelta-2", proposal_id="p1")
        _seed_pre_delta_proposal(fake_dynamo, namespace="ns-only-predelta-2", proposal_id="p2")

        rows = ds.list_proposals(namespace="ns-only-predelta-2", source_type="STRUCTURED")
        ids = {r["proposal_id"] for r in rows}
        assert ids == {"p1", "p2"}


class TestListProposalsScanPaginationDrain:
    """The ``limit=None`` cleanup drain must cross scan pages, not truncate.

    ``list_proposals(limit=None)`` passes NO ``Limit`` to ``scan`` and loops on
    ``LastEvaluatedKey`` to exhaustion — used by the grounded-proposal cleanup so
    a large namespace can't silently lose rows. DynamoDB paginates a scan on its
    own (≤1 MB/page) even with no ``Limit``; the fake models that via
    ``page_size``. Without prod's drain loop this returns only page 1 and the
    page-2 proposal is silently dropped.
    """

    def test_limit_none_drains_proposal_on_page_two(self, monkeypatch):
        from coa_ontology import dynamo_store as ds

        # page_size=2 forces a two-page scan for three matching proposals.
        table = _FakeDynamoTable(page_size=2)
        ns = "ns-paged"
        _seed_pre_delta_proposal(table, namespace=ns, proposal_id="p1")
        _seed_pre_delta_proposal(table, namespace=ns, proposal_id="p2")
        _seed_pre_delta_proposal(table, namespace=ns, proposal_id="p3")  # lands on page 2
        monkeypatch.setattr(ds, "_get_table", lambda: table)

        rows = ds.list_proposals(namespace=ns, limit=None)

        # All three, incl. the page-2 row. A single-page scan would drop p3.
        assert {r["proposal_id"] for r in rows} == {"p1", "p2", "p3"}


class TestListNamespacesScanPagination:
    """``list_namespaces`` must follow ``LastEvaluatedKey``.

    DynamoDB applies ``Limit`` to items EVALUATED before filtering, not to matches
    returned. This single-table design stores job / proposal / ingest-job rows
    alongside the ``NAMESPACE#`` ones, so once the table holds more non-namespace
    rows than the limit, a single-page scan returns few or zero namespaces and
    silently omits the rest — driven by TOTAL item count, not namespace count.
    """

    @staticmethod
    def _seed_namespace(table, name: str) -> None:
        table.put_item(Item={"PK": f"NAMESPACE#{name}", "SK": "META", "namespace": name})

    @staticmethod
    def _seed_noise(table, n: int) -> None:
        """Non-namespace rows of the kind that accumulate in normal use."""
        for i in range(n):
            table.put_item(Item={"PK": f"NS#x#JOB#{i}", "SK": "STATUS", "job_id": str(i)})

    def test_namespace_on_a_later_page_is_returned(self, monkeypatch):
        from coa_ontology import dynamo_store as ds

        table = _FakeDynamoTable(page_size=2)
        self._seed_namespace(table, "ns-a")
        self._seed_namespace(table, "ns-b")
        self._seed_namespace(table, "ns-c")  # lands on page 2
        monkeypatch.setattr(ds, "_get_table", lambda: table)

        rows = ds.list_namespaces()

        assert {r["namespace"] for r in rows} == {"ns-a", "ns-b", "ns-c"}

    def test_namespaces_hidden_behind_unrelated_rows_are_still_found(self, monkeypatch):
        """The actual production failure mode.

        The old code passed ``Limit=limit`` (100), and DynamoDB applies Limit to
        items EVALUATED, so with >100 job/proposal rows ahead of the namespace rows
        the single page it read contained no namespaces at all. Seeds past the
        default limit so the first evaluated page is entirely noise.
        """
        from coa_ontology import dynamo_store as ds

        table = _FakeDynamoTable(page_size=40)
        self._seed_noise(table, 150)  # more than the default limit of 100
        self._seed_namespace(table, "ns-late")
        monkeypatch.setattr(ds, "_get_table", lambda: table)

        rows = ds.list_namespaces()

        assert {r["namespace"] for r in rows} == {"ns-late"}

    def test_limit_still_caps_the_result(self, monkeypatch):
        from coa_ontology import dynamo_store as ds

        table = _FakeDynamoTable(page_size=2)
        for i in range(5):
            self._seed_namespace(table, f"ns-{i}")
        monkeypatch.setattr(ds, "_get_table", lambda: table)

        assert len(ds.list_namespaces(limit=3)) == 3

    def test_only_namespace_meta_rows_are_returned(self, monkeypatch):
        from coa_ontology import dynamo_store as ds

        table = _FakeDynamoTable(page_size=3)
        self._seed_namespace(table, "ns-a")
        self._seed_noise(table, 4)
        # A NAMESPACE# partition row that is not the META row must not match.
        table.put_item(Item={"PK": "NAMESPACE#ns-a", "SK": "OTHER", "namespace": "ns-a"})
        monkeypatch.setattr(ds, "_get_table", lambda: table)

        rows = ds.list_namespaces()

        assert [r["SK"] for r in rows] == ["META"]

    def test_empty_table_returns_empty(self, monkeypatch):
        from coa_ontology import dynamo_store as ds

        monkeypatch.setattr(ds, "_get_table", lambda: _FakeDynamoTable(page_size=2))

        assert ds.list_namespaces() == []


class TestListProposalsCrossNamespaceContainsFilter:
    """``list_proposals(namespace=None)`` filters on ``contains(PK, "#PROPOSAL#")``.

    The namespace-less scan (admin cross-namespace listing) matches proposal rows
    in ANY namespace by the ``#PROPOSAL#`` PK marker while excluding non-proposal
    partitions (e.g. JOB rows). Exercises the fake's ``contains()`` clause handler
    and the prod ``contains`` filter branch.
    """

    def test_returns_proposals_from_all_namespaces_excludes_jobs(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        _seed_pre_delta_proposal(fake_dynamo, namespace="ns-a", proposal_id="pa")
        _seed_pre_delta_proposal(fake_dynamo, namespace="ns-b", proposal_id="pb")
        # A JOB row (no ``#PROPOSAL#`` marker, SK != META) must NOT match.
        fake_dynamo.put_item(Item={"PK": ds._job_pk("ns-a", "j1"), "SK": "STATUS", "job_id": "j1"})

        rows = ds.list_proposals(namespace=None)

        assert {r["proposal_id"] for r in rows} == {"pa", "pb"}


# ── put_job: source_type default + persistence ─────────────────────────


class TestPutJobSourceType:
    def test_default_source_type_is_structured(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_job(
            job_id="j-default",
            status="pending",
            request_body={"ontology_uri_prefix": "https://example.com/onto"},
            datasource_details=[],
            namespace="ns-jobs",
        )

        item = ds.get_job("j-default", namespace="ns-jobs")
        assert item is not None
        assert item["source_type"] == "STRUCTURED"

    def test_explicit_unstructured_is_persisted(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_job(
            job_id="j-unstr",
            status="pending",
            request_body={"ontology_uri_prefix": "https://example.com/onto"},
            datasource_details=[],
            namespace="ns-jobs",
            source_type="UNSTRUCTURED",
        )

        item = ds.get_job("j-unstr", namespace="ns-jobs")
        assert item is not None
        assert item["source_type"] == "UNSTRUCTURED"


# ── list_jobs: source_type filter semantics ────────────────────────────


class TestListJobsSourceTypeFilter:
    """Same asymmetric backfill as ``list_proposals`` but on the JOB partition."""

    @pytest.fixture()
    def seeded(self, fake_dynamo):
        """Three jobs in one namespace: structured, unstructured, pre-delta."""
        from coa_ontology import dynamo_store as ds

        ns = "ns-list-jobs"
        ds.put_job(
            job_id="j-structured",
            status="pending",
            request_body={"ontology_uri_prefix": "https://example.com/onto"},
            datasource_details=[],
            namespace=ns,
            source_type="STRUCTURED",
        )
        ds.put_job(
            job_id="j-unstructured",
            status="pending",
            request_body={"ontology_uri_prefix": "https://example.com/onto"},
            datasource_details=[],
            namespace=ns,
            source_type="UNSTRUCTURED",
        )
        _seed_pre_delta_job(fake_dynamo, namespace=ns, job_id="j-predelta")
        return ns

    def test_no_filter_returns_all_three(self, seeded):
        from coa_ontology import dynamo_store as ds

        rows = ds.list_jobs(namespace=seeded)
        ids = {r["job_id"] for r in rows}
        assert ids == {"j-structured", "j-unstructured", "j-predelta"}

    def test_structured_filter_includes_predelta(self, seeded):
        """Pre-delta job items lack the attribute and are matched as STRUCTURED."""
        from coa_ontology import dynamo_store as ds

        rows = ds.list_jobs(namespace=seeded, source_type="STRUCTURED")
        ids = {r["job_id"] for r in rows}
        assert ids == {"j-structured", "j-predelta"}
        assert "j-unstructured" not in ids

    def test_unstructured_filter_excludes_predelta(self, seeded):
        """The UNSTRUCTURED filter never matches job items missing the attribute."""
        from coa_ontology import dynamo_store as ds

        rows = ds.list_jobs(namespace=seeded, source_type="UNSTRUCTURED")
        ids = {r["job_id"] for r in rows}
        assert ids == {"j-unstructured"}

    def test_status_filter_combines_with_source_type(self, fake_dynamo):
        """The ``status`` and ``source_type`` filters AND together correctly."""
        from coa_ontology import dynamo_store as ds

        ns = "ns-status-and-source"
        ds.put_job(
            job_id="j-pending-unstr",
            status="pending",
            request_body={"ontology_uri_prefix": "https://example.com/onto"},
            datasource_details=[],
            namespace=ns,
            source_type="UNSTRUCTURED",
        )
        ds.put_job(
            job_id="j-completed-unstr",
            status="completed",
            request_body={"ontology_uri_prefix": "https://example.com/onto"},
            datasource_details=[],
            namespace=ns,
            source_type="UNSTRUCTURED",
        )
        ds.put_job(
            job_id="j-pending-str",
            status="pending",
            request_body={"ontology_uri_prefix": "https://example.com/onto"},
            datasource_details=[],
            namespace=ns,
            source_type="STRUCTURED",
        )

        rows = ds.list_jobs(namespace=ns, status="pending", source_type="UNSTRUCTURED")
        ids = {r["job_id"] for r in rows}
        assert ids == {"j-pending-unstr"}


# ── update_job_status: reserved-keyword aliasing (Requirement 5.7) ─────


class TestUpdateJobStatusReservedKeyword:
    """The worker failure path calls ``update_job_status(..., error=...)``.

    ``error`` is a DynamoDB reserved keyword. If the SET clause embeds the
    bare attribute name, DynamoDB rejects the write with
    ``ValidationException: Attribute name is a reserved keyword`` and the
    ``failed`` status + error message are silently lost. The fix aliases
    every ``**extra`` name through ``ExpressionAttributeNames``.

    Requirements: 5.7
    """

    def test_failure_update_with_error_persists_and_reads_back(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ns = "ns-fail"
        ds.put_job(
            job_id="j-fail",
            status="pending",
            request_body={"ontology_uri_prefix": "https://example.com/onto"},
            datasource_details=[],
            namespace=ns,
            source_type="UNSTRUCTURED",
        )

        # The failure path: status="failed" carrying the reserved-keyword
        # ``error`` attribute. This must not raise and must persist.
        ds.update_job_status(
            "j-fail",
            status="failed",
            namespace=ns,
            error="RuntimeError: boom while inducing",
        )

        item = ds.get_job("j-fail", namespace=ns)
        assert item is not None
        assert item["status"] == "failed"
        assert item["error"] == "RuntimeError: boom while inducing"

    def test_error_attribute_is_aliased_in_update_expression(self, fake_dynamo, monkeypatch):
        """The bare reserved keyword never appears unaliased in the SET clause.

        Captures the raw ``update_item`` call to prove ``error`` is routed
        through ``ExpressionAttributeNames`` as an aliased ``#``-placeholder
        rather than embedded as ``error = :error`` (which real DynamoDB
        rejects).
        """
        from coa_ontology import dynamo_store as ds

        captured: dict[str, Any] = {}

        real_update = fake_dynamo.update_item

        def _spy(Key, UpdateExpression, **kwargs):
            captured["UpdateExpression"] = UpdateExpression
            captured["ExpressionAttributeNames"] = kwargs.get("ExpressionAttributeNames", {})
            return real_update(Key, UpdateExpression, **kwargs)

        monkeypatch.setattr(fake_dynamo, "update_item", _spy)

        ds.update_job_status(
            "j-alias",
            status="failed",
            namespace="ns-alias",
            error="ValueError: bad input",
        )

        names = captured["ExpressionAttributeNames"]
        # ``error`` must be aliased: some "#..." placeholder maps back to it.
        assert "error" in names.values()
        # The SET clause must reference ``error`` only via its ``#``-aliased
        # placeholder. A bare ``error =`` LHS (not preceded by ``#`` or ``:``)
        # is what real DynamoDB rejects — assert it never appears.
        expr = captured["UpdateExpression"]
        assert "#error = :error" in expr
        assert re.search(r"(?<![#:])\berror\s*=", expr) is None


class TestUpdateOntologyRegistryReservedKeyword:
    """``update_ontology_registry`` must alias EVERY attribute name.

    Ontology rows carry several DynamoDB reserved words — ``status``, ``name``,
    and (the one that broke the async-upload stub write) ``format``. The old
    hand-curated reserved set only covered {status, namespace, name}, so a
    ``format=...`` field produced a bare ``format = :format`` SET clause and
    DynamoDB rejected it with ``ValidationException: reserved keyword: format``,
    500-ing the upload. Aliasing every name is the future-proof fix.
    """

    def test_format_and_all_fields_are_aliased(self, fake_dynamo, monkeypatch):
        from coa_ontology import dynamo_store as ds

        captured: dict[str, Any] = {}
        real_update = fake_dynamo.update_item

        def _spy(Key, UpdateExpression, **kwargs):
            captured["UpdateExpression"] = UpdateExpression
            captured["ExpressionAttributeNames"] = kwargs.get("ExpressionAttributeNames", {})
            return real_update(Key, UpdateExpression, **kwargs)

        monkeypatch.setattr(fake_dynamo, "update_item", _spy)

        ds.update_ontology_registry(
            "ns-fmt",
            "https://example.org/o",
            status="ingesting",
            format="turtle",
            title="My Onto",
            parse_status="pending",
        )

        expr = captured["UpdateExpression"]
        names = captured["ExpressionAttributeNames"]
        # Every field (incl. the reserved ``format`` and ``status``) is aliased;
        # no bare reserved keyword appears as a SET left-hand side.
        assert "format" in names.values()
        assert "status" in names.values()
        assert re.search(r"(?<![#:])\bformat\s*=", expr) is None
        assert re.search(r"(?<![#:])\bstatus\s*=", expr) is None

    def test_write_persists_and_reads_back(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.update_ontology_registry(
            "ns-fmt2",
            "https://example.org/o2",
            status="ingesting",
            format="turtle",
            title="Stub Onto",
            parse_status="pending",
        )
        row = ds.get_ontology_registry("ns-fmt2", "https://example.org/o2")
        assert row is not None
        assert row["status"] == "ingesting"
        assert row["format"] == "turtle"


class TestPutOntologyRegistryClearsStubStatus:
    """A full ``put_ontology_registry`` write completes transient lifecycle state.

    The async-upload stub row carries ``status="ingesting"``. When the background
    ingest finishes, ``ingest_ontology`` calls ``put_ontology_registry`` with the
    fully-ingested data (which has no ``status`` key). That transient status must
    be dropped so the row doesn't look perpetually in-flight — a fully-loaded
    ontology has no ``status`` (the shape every normal row has).
    """

    def test_fresh_write_clears_ingesting_stub_status(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ns, oid = "ns-clear", "https://example.org/o"
        # Stub row written at upload-trigger time.
        ds.update_ontology_registry(ns, oid, status="ingesting", parse_status="pending", format="turtle")
        assert ds.get_ontology_registry(ns, oid)["status"] == "ingesting"

        # Background ingest completes → full write with no status key.
        ds.put_ontology_registry(
            ns, oid, {"uri": oid, "title": "Done", "parse_status": "ok", "class_count": 3, "embedding_count": 5}
        )
        row = ds.get_ontology_registry(ns, oid)
        assert "status" not in row or row.get("status") in (None, "")
        assert row["parse_status"] == "ok"
        assert row["class_count"] == 3

    def test_explicit_status_in_data_is_respected(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ns, oid = "ns-clear2", "https://example.org/o2"
        ds.update_ontology_registry(ns, oid, status="ingesting")
        # A caller that DOES supply status keeps it (only the implicit-clear path
        # drops a lingering transient status).
        ds.put_ontology_registry(ns, oid, {"uri": oid, "status": "deleting"})
        assert ds.get_ontology_registry(ns, oid)["status"] == "deleting"


# ── Async ontology-ingest job persistence ──────────────────────────────


class TestIngestJobPersistence:
    """Async ontology-ingest jobs persist status to DynamoDB.

    Previously the ingest router kept job state in a process-local dict, so
    the ``GET .../ingest-status/{job_id}`` poll 404'd whenever the worker
    task and the polling request landed on different ECS task instances, or
    when the task recycled mid-ingest. These helpers move the state to the
    ``#INGESTJOB#`` DynamoDB partition; the tests cover the create →
    running → terminal lifecycle and the reserved-keyword ``error`` path.
    """

    def test_put_then_get_roundtrip(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_ingest_job(
            job_id="ij-1",
            ontology_id="https://schema.org/async-test",
            status="pending",
            namespace="ns-ingest",
        )

        item = ds.get_ingest_job("ij-1", namespace="ns-ingest")
        assert item is not None
        assert item["status"] == "pending"
        assert item["ontology_id"] == "https://schema.org/async-test"
        assert item["error"] is None
        assert item["result"] is None

    def test_get_missing_job_returns_none(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        assert ds.get_ingest_job("nope", namespace="ns-ingest") is None

    def test_update_to_completed_persists_result(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_ingest_job(
            job_id="ij-done",
            ontology_id="https://schema.org/async-test",
            status="pending",
            namespace="ns-ingest",
        )
        ds.update_ingest_job(
            "ij-done",
            "completed",
            namespace="ns-ingest",
            result={"ontology_id": "https://schema.org/async-test", "class_count": 12, "embedding_count": 14},
        )

        item = ds.get_ingest_job("ij-done", namespace="ns-ingest")
        assert item is not None
        assert item["status"] == "completed"
        assert item["result"] == {
            "ontology_id": "https://schema.org/async-test",
            "class_count": 12,
            "embedding_count": 14,
        }

    def test_update_failure_aliases_reserved_error_keyword(self, fake_dynamo, monkeypatch):
        """``status``/``error`` are DynamoDB reserved words. The DAO aliases
        every field name (``#a0``/``#a1`` …), so no bare reserved word appears
        in the SET clause and the failed status + error round-trip correctly."""
        from coa_ontology import dynamo_store as ds

        captured: dict[str, Any] = {}
        real_update = fake_dynamo.update_item

        def _spy(Key, UpdateExpression, **kwargs):
            captured["UpdateExpression"] = UpdateExpression
            return real_update(Key, UpdateExpression, **kwargs)

        monkeypatch.setattr(fake_dynamo, "update_item", _spy)

        ds.put_ingest_job(
            job_id="ij-fail",
            ontology_id="https://schema.org/async-test",
            status="pending",
            namespace="ns-ingest",
        )
        ds.update_ingest_job(
            "ij-fail",
            "failed",
            namespace="ns-ingest",
            error="Parse error: not valid RDF/XML",
        )

        # Every LHS in the SET clause is an aliased ``#``-placeholder — no bare
        # ``status =`` or ``error =`` that real DynamoDB would reject.
        expr = captured["UpdateExpression"]
        assert re.search(r"(?<![#:\w])(status|error)\s*=", expr) is None

        item = ds.get_ingest_job("ij-fail", namespace="ns-ingest")
        assert item is not None
        assert item["status"] == "failed"
        assert item["error"] == "Parse error: not valid RDF/XML"

    def test_ingest_jobs_isolated_from_induction_list_jobs(self, fake_dynamo):
        """Ingest jobs use the ``#INGESTJOB#`` partition and must NOT leak into
        the induction ``list_jobs`` scan (which begins-with ``#JOB#``)."""
        from coa_ontology import dynamo_store as ds

        ns = "ns-isolation"
        ds.put_ingest_job(
            job_id="ij-iso",
            ontology_id="https://schema.org/async-test",
            status="pending",
            namespace=ns,
        )
        ds.put_job(
            job_id="induce-iso",
            status="pending",
            request_body={"ontology_uri_prefix": "https://example.com/onto"},
            datasource_details=[],
            namespace=ns,
        )

        induction_jobs = ds.list_jobs(namespace=ns)
        ids = {r["job_id"] for r in induction_jobs}
        assert ids == {"induce-iso"}
        assert "ij-iso" not in ids

    def test_list_ingest_jobs_returns_only_this_namespace(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_ingest_job(job_id="ij-1", ontology_id="https://schema.org/a", status="pending", namespace="ns-a")
        ds.put_ingest_job(job_id="ij-2", ontology_id="https://schema.org/b", status="running", namespace="ns-a")
        ds.put_ingest_job(job_id="ij-3", ontology_id="https://schema.org/c", status="pending", namespace="ns-b")

        result = ds.list_ingest_jobs(namespace="ns-a")

        ids = {r["job_id"] for r in result}
        assert ids == {"ij-1", "ij-2"}

    def test_list_ingest_jobs_filters_by_status(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_ingest_job(job_id="ij-1", ontology_id="https://schema.org/a", status="completed", namespace="ns-a")
        ds.put_ingest_job(job_id="ij-2", ontology_id="https://schema.org/b", status="running", namespace="ns-a")

        result = ds.list_ingest_jobs(namespace="ns-a", status="running")

        assert len(result) == 1
        assert result[0]["job_id"] == "ij-2"

    def test_list_ingest_jobs_does_not_leak_across_namespaces(self, fake_dynamo):
        """PK prefix match must not accidentally match a different namespace
        that shares a prefix (e.g. 'ns-a' vs 'ns-ab')."""
        from coa_ontology import dynamo_store as ds

        ds.put_ingest_job(job_id="ij-1", ontology_id="https://schema.org/a", status="pending", namespace="ns-a")
        ds.put_ingest_job(job_id="ij-2", ontology_id="https://schema.org/b", status="pending", namespace="ns-ab")

        result = ds.list_ingest_jobs(namespace="ns-a")

        ids = {r["job_id"] for r in result}
        assert ids == {"ij-1"}

    def test_enum_status_stored_as_wire_value_not_repr(self, fake_dynamo):
        """Regression: IngestJobState is a plain (str, Enum), so ``str(member)``
        is ``'IngestJobState.RUNNING'`` — the stored status MUST be the wire
        value ``'running'`` so the item re-validates as an IngestJob (the poll
        500'd with a pydantic enum ValidationError before this fix)."""
        from coa_control_plane_server.models.ingest_job import IngestJob
        from coa_control_plane_server.models.ingest_job_state import IngestJobState
        from coa_ontology import dynamo_store as ds

        ds.put_ingest_job(
            job_id="ij-enum",
            ontology_id="https://schema.org/x",
            status=IngestJobState.PENDING,
            namespace="ns-enum",
        )
        ds.update_ingest_job("ij-enum", IngestJobState.RUNNING, namespace="ns-enum")
        item = ds.get_ingest_job("ij-enum", namespace="ns-enum")
        assert item["status"] == "running"  # NOT "IngestJobState.RUNNING"
        # And the stored item must round-trip back into a typed IngestJob.
        assert IngestJob.model_validate(item).status == IngestJobState.RUNNING


# ── Generic async-job persistence + staleness sweep (Theme B) ──────────────


class TestAsyncJobPersistence:
    def test_put_get_roundtrip(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_async_job("VALIDATE", "j1", {"job_id": "j1", "proposal_id": "p1", "status": "pending"}, namespace="ns")
        item = ds.get_async_job("VALIDATE", "j1", namespace="ns")
        assert item is not None
        assert item["status"] == "pending"
        assert item["proposal_id"] == "p1"

    def test_kind_partitions_do_not_collide(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_async_job("VALIDATE", "same-id", {"job_id": "same-id", "status": "completed"}, namespace="ns")
        ds.put_async_job("INFER", "same-id", {"job_id": "same-id", "status": "failed"}, namespace="ns")
        assert ds.get_async_job("VALIDATE", "same-id", namespace="ns")["status"] == "completed"
        assert ds.get_async_job("INFER", "same-id", namespace="ns")["status"] == "failed"

    def test_update_changes_status(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_async_job("INFER", "j2", {"job_id": "j2", "status": "pending"}, namespace="ns")
        ds.update_async_job("INFER", "j2", {"status": "completed", "result": {"ok": True}}, namespace="ns")
        item = ds.get_async_job("INFER", "j2", namespace="ns")
        assert item["status"] == "completed"
        assert item["result"] == {"ok": True}

    def test_missing_job_returns_none(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        assert ds.get_async_job("VALIDATE", "nope", namespace="ns") is None

    def test_stale_nonterminal_job_swept_to_failed(self, fake_dynamo, monkeypatch):
        # A running job whose updated_at is older than the stale window is
        # reported as failed on read (its worker died with a recycled task).
        from datetime import UTC, datetime, timedelta

        from coa_ontology import dynamo_store as ds

        monkeypatch.setattr(ds, "_STALE_JOB_SECONDS", 60)
        old = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        ds.put_async_job("VALIDATE", "j3", {"job_id": "j3", "status": "running"}, namespace="ns")
        # Backdate updated_at directly on the STORED item (get_item returns a
        # copy) to simulate an orphaned job — update_async_job always stamps a
        # fresh updated_at, so it can't be used to age a row.
        pk = ds._async_job_pk("ns", "VALIDATE", "j3")
        fake_dynamo.items[(pk, "STATUS")]["updated_at"] = old

        item = ds.get_async_job("VALIDATE", "j3", namespace="ns")
        assert item["status"] == "failed"
        assert "did not complete" in item["error"]
        # The sweep is persisted, not just returned.
        assert fake_dynamo.items[(pk, "STATUS")]["status"] == "failed"

    def test_fresh_nonterminal_job_not_swept(self, fake_dynamo, monkeypatch):
        from coa_ontology import dynamo_store as ds

        monkeypatch.setattr(ds, "_STALE_JOB_SECONDS", 3600)
        ds.put_async_job("INFER", "j4", {"job_id": "j4", "status": "running"}, namespace="ns")
        assert ds.get_async_job("INFER", "j4", namespace="ns")["status"] == "running"

    def test_terminal_job_never_swept(self, fake_dynamo, monkeypatch):
        from datetime import UTC, datetime, timedelta

        from coa_ontology import dynamo_store as ds

        monkeypatch.setattr(ds, "_STALE_JOB_SECONDS", 60)
        old = (datetime.now(UTC) - timedelta(seconds=999)).isoformat()
        ds.put_async_job("INFER", "j5", {"job_id": "j5", "status": "completed"}, namespace="ns")
        ds.update_async_job("INFER", "j5", {"updated_at": old}, namespace="ns")
        # Completed stays completed even though it is old.
        assert ds.get_async_job("INFER", "j5", namespace="ns")["status"] == "completed"

    def test_delete_all_namespace_items_purges_async_jobs(self, fake_dynamo):
        # Regression: delete_all_namespace_items must sweep the #ASYNCJOB#
        # partition too (added by the async validate/infer persistence work),
        # not just JOB/PROPOSAL/INGESTJOB — else persisted async jobs leak past a
        # namespace ontology wipe.
        from coa_ontology import dynamo_store as ds

        ns = "wipe-ns"
        # Seed one item in every partition the wipe must cover.
        ds.put_async_job("VALIDATE", "av", {"job_id": "av", "status": "completed"}, namespace=ns)
        ds.put_async_job("INFER", "ai", {"job_id": "ai", "status": "completed"}, namespace=ns)
        ds.put_ingest_job("ig", "http://o", "completed", namespace=ns)
        fake_dynamo.put_item(Item={"PK": ds._namespace_pk(ns), "SK": "ONTOLOGY#o", "x": 1})
        fake_dynamo.put_item(Item={"PK": ds._job_pk(ns, "jb"), "SK": "STATUS", "x": 1})
        fake_dynamo.put_item(Item={"PK": ds._proposal_pk(ns, "pr"), "SK": "META", "x": 1})

        deleted = ds.delete_all_namespace_items(ns)

        assert deleted == 6  # all partitions, incl. both async jobs
        # Async-job rows are gone (the partition that previously leaked).
        assert ds.get_async_job("VALIDATE", "av", namespace=ns) is None
        assert ds.get_async_job("INFER", "ai", namespace=ns) is None

    def test_delete_all_namespace_items_tears_down_job_datasources_s3(self, fake_dynamo):
        # Regression: put_job_datasources_s3 offloads a manifest to jobs/{ns}/…;
        # those blobs must be swept on namespace delete or they leak in S3.
        from coa_ontology import dynamo_store as ds

        ns = "wipe-ns"
        s3 = ds._get_s3()
        ds.put_job_datasources_s3(ns, "jb", [{"datasourceId": "db0", "tables": ["t1"]}])
        # Another namespace's blob must survive (prefix-scoped teardown only).
        ds.put_job_datasources_s3("other-ns", "jb", [{"datasourceId": "db0", "tables": ["t1"]}])
        assert any(key.startswith(f"jobs/{ns}/") for (_b, key) in s3.objects)

        ds.delete_all_namespace_items(ns)

        assert not any(key.startswith(f"jobs/{ns}/") for (_b, key) in s3.objects)
        assert any(key.startswith("jobs/other-ns/") for (_b, key) in s3.objects)


class TestDeleteProposal:
    """``delete_proposal`` — remove a proposal's DDB row + S3 artifacts."""

    def test_deletes_row_and_s3_artifacts(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ns, pid = "acme", "p-1"
        # Create a proposal (writes DDB row + S3 ontology/r2rml artifacts).
        ds.create_proposal(
            proposal_id=pid,
            job_id="job-1",
            namespace=ns,
            ontology_id="https://ex.org/o#",
            proposal_type="induction",
            ontology_turtle="@prefix ex: <https://ex.org/> .",
            r2rml_turtle="@prefix rr: <http://www.w3.org/ns/r2rml#> .",
        )
        # Precondition: row + at least one S3 object exist.
        assert ds.get_proposal_by_id(pid, namespace=ns, hydrate_turtle=False) is not None
        prefix = f"{ds._S3_PROPOSALS_PREFIX}/{ns}/{pid}/"
        s3 = ds._get_s3()
        assert any(key.startswith(prefix) for (_b, key) in s3.objects)

        assert ds.delete_proposal(ns, pid) is True

        # DDB row gone.
        assert ds.get_proposal_by_id(pid, namespace=ns, hydrate_turtle=False) is None
        # All S3 objects under the proposal prefix gone.
        assert not any(key.startswith(prefix) for (_b, key) in s3.objects)

    def test_returns_false_when_row_absent(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        assert ds.delete_proposal("acme", "does-not-exist") is False


class TestDeleteAllNamespaceItemsS3Sweep:
    """``delete_all_namespace_items`` must also sweep offloaded job manifests.

    Regression: #797 offloads job datasources to ``jobs/{ns}/{id}/datasources.json``
    (put_job_datasources_s3), but the namespace wipe only deleted DDB rows —
    leaking those S3 blobs. The wipe now sweeps the ``jobs/{ns}/`` prefix too.
    """

    def test_sweeps_jobs_prefix_and_returns_ddb_count(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ns = "sweep-ns"
        # A job row in DDB plus its offloaded manifest in S3.
        fake_dynamo.put_item(Item={"PK": ds._job_pk(ns, "j1"), "SK": "STATUS", "x": 1})
        ds.put_job_datasources_s3(ns, "j1", [{"tables": ["t"]}])
        prefix = f"jobs/{ns}/"
        s3 = ds._get_s3()
        assert any(key.startswith(prefix) for (_b, key) in s3.objects)
        # An unrelated namespace's manifest must survive the wipe.
        ds.put_job_datasources_s3("other-ns", "j2", [{"tables": ["u"]}])

        deleted = ds.delete_all_namespace_items(ns)

        assert deleted == 1  # DDB count unchanged by the S3 sweep
        assert not any(key.startswith(prefix) for (_b, key) in s3.objects)
        assert any(key.startswith("jobs/other-ns/") for (_b, key) in s3.objects)

    def test_s3_failure_does_not_block_ddb_delete(self, fake_dynamo, monkeypatch):
        from botocore.exceptions import BotoCoreError
        from coa_ontology import dynamo_store as ds

        ns = "boom-ns"
        fake_dynamo.put_item(Item={"PK": ds._job_pk(ns, "j1"), "SK": "STATUS", "x": 1})
        ds.put_job_datasources_s3(ns, "j1", [{"tables": ["t"]}])

        # The S3 sweep raises an AWS-side error — it must be swallowed, and the
        # DDB deletion must still complete and return its count.
        def _boom(*_a, **_k):
            raise BotoCoreError()

        monkeypatch.setattr(ds._get_s3(), "get_paginator", _boom)

        deleted = ds.delete_all_namespace_items(ns)

        assert deleted == 1  # DDB row still deleted despite the S3 failure
        assert fake_dynamo.items.get((ds._job_pk(ns, "j1"), "STATUS")) is None


class _PaginatingQueryTable:
    """Fake Dynamo Table whose ``query`` returns items one page at a time,
    honouring ``ExclusiveStartKey`` — enough to exercise the pagination loop in
    ``list_ontologies_registry``. FilterExpression is applied per page AFTER the
    page is sliced (mirroring real DynamoDB: filter runs on the ≤1 MB the page
    read), so a matching row on a later page is invisible to a single query.
    """

    def __init__(self, rows: list[dict], page_size: int) -> None:
        # Preserve insertion order; page boundaries are index-based.
        self._rows = rows
        self._page_size = page_size

    def query(self, **kwargs):
        start = int(kwargs.get("ExclusiveStartKey", {}).get("_i", 0))
        page = self._rows[start : start + self._page_size]
        end = start + len(page)

        # Apply the ontology_type FilterExpression AFTER paging (post-read),
        # exactly like DynamoDB.
        values = kwargs.get("ExpressionAttributeValues", {}) or {}
        want_type = values.get(":otype")
        matched = [r for r in page if want_type is None or r.get("ontology_type") == want_type]

        resp: dict = {"Items": [dict(r) for r in matched]}
        if end < len(self._rows):
            resp["LastEvaluatedKey"] = {"_i": end}
        return resp


class TestListOntologiesRegistryPagination:
    """Regression: the induced-ontology delete guard calls
    ``list_ontologies_registry(namespace, ontology_type="induced", limit=None)``.
    A single query page only reads ≤1 MB and applies the type filter afterward,
    so an induced row on a later page must NOT be missed — otherwise the guard
    silently allows a delete it should block."""

    def _rows(self):
        # Page 1 is all non-induced; the only induced row sits on page 2.
        return [
            {"PK": "NAMESPACE#ns", "SK": "ONTOLOGY#a", "ontology_id": "a", "ontology_type": "user_created"},
            {"PK": "NAMESPACE#ns", "SK": "ONTOLOGY#b", "ontology_id": "b", "ontology_type": "foundational"},
            {"PK": "NAMESPACE#ns", "SK": "ONTOLOGY#c", "ontology_id": "c", "ontology_type": "induced"},
        ]

    def test_drains_all_pages_when_limit_none(self, monkeypatch):
        from coa_ontology import dynamo_store as ds

        table = _PaginatingQueryTable(self._rows(), page_size=2)
        monkeypatch.setattr(ds, "_get_table", lambda: table)

        induced = ds.list_ontologies_registry("ns", ontology_type="induced", limit=None)

        # Without the LastEvaluatedKey loop this returns [] (induced row is on
        # page 2, page 1 filters to nothing) and the guard would wrongly pass.
        assert [r["ontology_id"] for r in induced] == ["c"]

    def test_unfiltered_drain_returns_every_row_across_pages(self, monkeypatch):
        from coa_ontology import dynamo_store as ds

        table = _PaginatingQueryTable(self._rows(), page_size=2)
        monkeypatch.setattr(ds, "_get_table", lambda: table)

        rows = ds.list_ontologies_registry("ns", limit=None)

        assert {r["ontology_id"] for r in rows} == {"a", "b", "c"}

    def test_positive_limit_uses_single_page_fast_path(self, monkeypatch):
        from coa_ontology import dynamo_store as ds

        # page_size larger than limit: one query, capped to limit.
        table = _PaginatingQueryTable(self._rows(), page_size=10)
        monkeypatch.setattr(ds, "_get_table", lambda: table)

        rows = ds.list_ontologies_registry("ns", limit=2)

        assert len(rows) == 2


# ── Workstream A: orphaned-job deterministic recovery ──────────────────────
#
# A1 startup reconcile, A2 liveness heartbeat, A3 terminal-union self-heal.
# A hard worker death (OOM SIGKILL, exit 137) runs neither the worker's
# ``except`` nor its ``finally``, freezing the DDB row at ``building_ontology``
# and never releasing the namespace lock. These tests prove the recovery path
# from that frozen state — all offline against the in-memory fake table.


def _seed_induction_job(
    table: _FakeDynamoTable,
    *,
    namespace: str,
    job_id: str,
    status: str,
    age_seconds: float = 0.0,
) -> None:
    """Seed a ``{ns}#JOB#{id}`` / STATUS induction row with an aged ``updated_at``.

    ``age_seconds`` backdates ``updated_at`` so the age-gated sweep sees the row
    as stale (orphaned) without any wall-clock waiting.
    """
    from datetime import UTC, datetime, timedelta

    stamp = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    table.put_item(
        Item={
            "PK": f"{namespace}#JOB#{job_id}",
            "SK": "STATUS",
            "job_id": job_id,
            "namespace": namespace,
            "status": status,
            "created_at": stamp,
            "updated_at": stamp,
        }
    )


class TestInductionWatchdog:
    """A2: the liveness heartbeat is deterministic and touch-only (never clobbers)."""

    def test_watchdog_ticks_deterministically_and_touch_only(self, fake_dynamo):
        """Drive ``_watchdog_loop`` with a counting stop-event (no wall clock):
        it must call ``touch_job`` exactly once per tick, and a tick that lands
        after a terminal write must never resurrect the finished status."""
        import time

        import coa_ontology.induce_catalog as ic
        from coa_ontology import dynamo_store as ds

        # Part A — N ticks → N touch_job calls, deterministically.
        class _NTickEvent:
            def __init__(self, ticks: int) -> None:
                self.ticks = ticks
                self.calls = 0

            def wait(self, _timeout) -> bool:
                self.calls += 1
                # False for the first ``ticks`` calls (a heartbeat each), then
                # True to break the ``while not stop_event.wait(...)`` loop.
                return self.calls > self.ticks

        touch_calls: list[tuple[str, str]] = []
        # Scoped patch: a bare ``monkeypatch.undo()`` here would also revert the
        # fake_dynamo fixture's _get_table patch (same monkeypatch instance), so
        # Part B would hit real boto3. A nested context restores ONLY touch_job.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ds, "touch_job", lambda job_id, namespace: touch_calls.append((job_id, namespace)))
            # Stub the per-tick OOM-observability emit — this test asserts touch
            # behavior, not metrics, and the real emit would make a live
            # PutMetricData call. (Emission is covered by test_watchdog_metric_emission.)
            mp.setattr(ic, "emit_induction_heartbeat_metrics", lambda *a, **k: None)
            ic._watchdog_loop("j-hb", "ns-hb", _NTickEvent(3), interval=0)

        assert touch_calls == [("j-hb", "ns-hb")] * 3

        # Part B — a late tick against a COMPLETED row only bumps updated_at
        # (real touch_job, restored on context exit; writes to the fake table).
        _seed_induction_job(fake_dynamo, namespace="ns-hb", job_id="j-done", status="completed")
        before = fake_dynamo.items[("ns-hb#JOB#j-done", "STATUS")]["updated_at"]
        time.sleep(0.001)  # ensure the ISO timestamp advances

        ds.touch_job("j-done", "ns-hb")  # a watchdog tick racing the terminal write

        row = fake_dynamo.items[("ns-hb#JOB#j-done", "STATUS")]
        assert row["status"] == "completed"  # status never clobbered
        assert row["updated_at"] != before  # only the liveness stamp moved


class TestCountActiveJobsSelfHeal:
    """A3: ``count_active_jobs`` self-heals a stale orphan with no restart."""

    def test_ingested_job_counts_zero_and_stale_row_self_heals(self, fake_dynamo, monkeypatch):
        import coa_ontology.induce_catalog as ic

        monkeypatch.setattr(ic, "_STALE_INDUCTION_SECONDS", 60)

        # An ``ingested`` job (terminal in the UNION) counts 0 active.
        _seed_induction_job(fake_dynamo, namespace="ns-ing", job_id="acc", status="ingested")
        assert ic.count_active_jobs("ns-ing") == {"activeJobs": 0}

        # A stale ``building_ontology`` orphan is swept to failed IN THE COUNTER
        # itself — namespace deletion self-heals on the next attempt, no restart.
        _seed_induction_job(
            fake_dynamo, namespace="ns-stale", job_id="orphan", status="building_ontology", age_seconds=120
        )
        assert ic.count_active_jobs("ns-stale") == {"activeJobs": 0}
        # The self-heal persisted, not just returned.
        assert fake_dynamo.items[("ns-stale#JOB#orphan", "STATUS")]["status"] == "failed"


class TestGetJobStaleSweep:
    """Regression for the EXISTING lazy ``get_job`` sweep (previously untested)."""

    def test_stale_building_ontology_is_swept_on_poll(self, fake_dynamo, monkeypatch):
        """A poll of a stale non-terminal row with no live worker on this task
        (empty ``_jobs``) sweeps it to ``failed`` and persists the flip, so the
        client poll terminates instead of spinning on a dead job."""
        import coa_ontology.induce_catalog as ic

        monkeypatch.setattr(ic, "_jobs", {})  # no in-memory worker → DDB fallback
        monkeypatch.setattr(ic, "_STALE_INDUCTION_SECONDS", 60)
        _seed_induction_job(
            fake_dynamo, namespace="ns-poll", job_id="orphan", status="building_ontology", age_seconds=120
        )

        result = ic.get_job("orphan", namespace="ns-poll")

        assert result["status"] == "failed"
        assert result["error"] == ic._STALE_JOB_ERROR
        # Persisted, not just returned.
        assert fake_dynamo.items[("ns-poll#JOB#orphan", "STATUS")]["status"] == "failed"


class TestSweepJobIfStaleGuards:
    """Regression for the two hardening guards on ``_sweep_job_if_stale`` — the
    shared age-gate on the namespace-deletion critical path (``count_active_jobs``
    and ``get_job`` both route through it), where a raise would abort deletion."""

    def test_missing_job_id_returns_unchanged_without_sweep(self, monkeypatch):
        """A stale-looking, non-terminal row with NO ``job_id`` must early-return
        unchanged and never raise — so a malformed row can't blow up the
        counter/poll. The age is stale AND the threshold is lowered so that,
        without the guard, the flip branch WOULD fire; the spy asserts
        ``update_job_status`` is never reached (no ``update_job_status(None, ...)``)."""
        from datetime import UTC, datetime, timedelta

        import coa_ontology.induce_catalog as ic

        monkeypatch.setattr(ic, "_STALE_INDUCTION_SECONDS", 60)
        calls: list = []
        monkeypatch.setattr(ic.dynamo_store, "update_job_status", lambda *a, **k: calls.append((a, k)))

        old_iso = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        item = {"status": "building_ontology", "updated_at": old_iso}  # no job_id

        result = ic._sweep_job_if_stale(item, "ns-malformed")

        # Early return before the age-gate: the flip never fires on a row with no
        # job_id, so no persist call and the status is left untouched.
        assert calls == []
        assert result["status"] == "building_ontology"
        assert "error" not in result

    def test_ddb_failure_during_flip_is_swallowed(self, monkeypatch):
        """A stale non-terminal row whose ``update_job_status`` raises (DDB
        throttle/ClientError) must NOT propagate out of ``_sweep_job_if_stale``,
        and the item's status stays non-terminal — the flip did not apply, so the
        next sweep retries instead of the exception aborting namespace deletion."""
        from datetime import UTC, datetime, timedelta

        import coa_ontology.induce_catalog as ic

        monkeypatch.setattr(ic, "_STALE_INDUCTION_SECONDS", 60)

        def _raise(*_args, **_kwargs):
            raise Exception("simulated DDB throttle")

        monkeypatch.setattr(ic.dynamo_store, "update_job_status", _raise)

        old_iso = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        item = {"job_id": "orphan", "status": "building_ontology", "updated_at": old_iso}

        # Must return without raising even though the persist throws.
        result = ic._sweep_job_if_stale(item, "ns-throttle")

        # Flip was NOT applied to the item (the raise beat the mutation) → the
        # row is left non-terminal for the next sweep to retry.
        assert result["status"] == "building_ontology"
        assert "error" not in result


class TestSweepProposalIfStale:
    """#466/#456: a proposal orphaned by a dead accept worker (stuck in
    ``accepting``/``embeddings_sync``) must be moved to the terminal
    ``accept_failed`` state with an ``accept_error`` on read, so the UI surfaces
    the failure and the user can re-accept (the pipeline is idempotent on re-run).
    Mirrors the async-job stale sweep. Fresh in-progress proposals are untouched.
    """

    def _seed(self, table, *, proposal_id, namespace, status, updated_at):
        table.put_item(
            Item={
                "PK": f"{namespace}#PROPOSAL#{proposal_id}",
                "SK": "META",
                "proposal_id": proposal_id,
                "job_id": f"job-{proposal_id}",
                "namespace": namespace,
                "ontology_id": "https://example.com/onto",
                "proposal_type": "induction",
                "status": status,
                "updated_at": updated_at,
                "ontology_turtle": "",
                "r2rml_turtle": "",
            }
        )

    def _iso_ago(self, seconds):
        from datetime import UTC, datetime, timedelta

        return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()

    def test_stale_accepting_is_marked_accept_failed(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        self._seed(
            fake_dynamo,
            proposal_id="p1",
            namespace="ns",
            status="accepting",
            updated_at=self._iso_ago(ds._ACCEPT_STALE_SECONDS + 60),
        )
        item = ds.get_proposal_by_id("p1", namespace="ns", hydrate_turtle=False)
        # A dead worker lands the proposal in the terminal ``accept_failed``
        # state (distinct from ``pending`` so the UI shows "accept failed,
        # re-accept to retry" rather than a fresh proposal).
        assert item["status"] == "accept_failed"
        assert item["accept_error"]  # reason surfaced for the UI
        # Persisted, not just returned: a second read sees accept_failed too.
        again = ds.get_proposal_by_id("p1", namespace="ns", hydrate_turtle=False)
        assert again["status"] == "accept_failed"

    def test_stale_embeddings_sync_is_marked_accept_failed(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        self._seed(
            fake_dynamo,
            proposal_id="p2",
            namespace="ns",
            status="embeddings_sync",
            updated_at=self._iso_ago(ds._ACCEPT_STALE_SECONDS + 60),
        )
        item = ds.get_proposal_by_id("p2", namespace="ns", hydrate_turtle=False)
        assert item["status"] == "accept_failed"

    def test_fresh_accepting_is_not_swept(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        self._seed(
            fake_dynamo,
            proposal_id="p3",
            namespace="ns",
            status="accepting",
            updated_at=self._iso_ago(10),  # well within the window — worker is alive
        )
        item = ds.get_proposal_by_id("p3", namespace="ns", hydrate_turtle=False)
        assert item["status"] == "accepting"
        assert not item.get("accept_error")

    def test_terminal_status_is_never_swept(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        # An old ``accepted`` proposal must stay accepted regardless of age.
        self._seed(
            fake_dynamo,
            proposal_id="p4",
            namespace="ns",
            status="accepted",
            updated_at=self._iso_ago(ds._ACCEPT_STALE_SECONDS + 10_000),
        )
        item = ds.get_proposal_by_id("p4", namespace="ns", hydrate_turtle=False)
        assert item["status"] == "accepted"

    def test_missing_timestamp_is_not_swept(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        # No updated_at/created_at → can't judge staleness → leave as-is.
        fake_dynamo.put_item(
            Item={
                "PK": "ns#PROPOSAL#p5",
                "SK": "META",
                "proposal_id": "p5",
                "namespace": "ns",
                "status": "accepting",
                "ontology_turtle": "",
                "r2rml_turtle": "",
            }
        )
        item = ds.get_proposal_by_id("p5", namespace="ns", hydrate_turtle=False)
        assert item["status"] == "accepting"

    def test_heartbeat_keeps_a_slow_but_live_accept_from_being_swept(self, fake_dynamo):
        """The POINT of the per-step heartbeat: a long-running accept that is
        still making progress must never be swept.

        Without a heartbeat the sweep can't tell "worker died" from "worker is
        slow", so it would reset a LIVE accept to a re-acceptable state — the
        user re-accepts, and now TWO workers ingest the same ontology
        concurrently (duplicate embeddings / graph pollution, the #594 failure
        mode). The accept pipeline checkpoints every step via update_proposal,
        which refreshes ``updated_at``; this test proves that refresh actually
        defeats the staleness check.
        """
        from coa_ontology import dynamo_store as ds

        # The accept STARTED well past the stale window ago (created_at is old,
        # and so is the pre-heartbeat updated_at) — a long-running accept.
        old = self._iso_ago(ds._ACCEPT_STALE_SECONDS + 600)
        self._seed(
            fake_dynamo,
            proposal_id="p6",
            namespace="ns",
            status="accepting",
            updated_at=old,
        )
        # Seed created_at as stale too, so this test is sensitive to the sweep
        # reading the WRONG timestamp: judging staleness from created_at (or
        # ignoring the updated_at refresh) must fail this test.
        fake_dynamo.update_item(
            Key={"PK": "ns#PROPOSAL#p6", "SK": "META"},
            UpdateExpression="SET created_at = :c",
            ExpressionAttributeValues={":c": old},
        )
        # ...but the worker checkpoints a step (exactly what _run_accept_step
        # does), which stamps a fresh server-side updated_at.
        ds.update_proposal("p6", namespace="ns", accept_progress={"step": "ingest", "state": "done"})

        item = ds.get_proposal_by_id("p6", namespace="ns", hydrate_turtle=False)
        # Still in progress — NOT swept, no error injected.
        assert item["status"] == "accepting"
        assert not item.get("accept_error")

    def test_stale_window_is_measured_from_the_last_heartbeat(self, fake_dynamo):
        """Complement of the above: once the heartbeats STOP (worker died), the
        clock runs from the last checkpoint and the proposal is swept."""
        from coa_ontology import dynamo_store as ds

        self._seed(
            fake_dynamo,
            proposal_id="p7",
            namespace="ns",
            status="embeddings_sync",
            # Last heartbeat was longer ago than the window → worker is gone.
            updated_at=self._iso_ago(ds._ACCEPT_STALE_SECONDS + 1),
        )
        item = ds.get_proposal_by_id("p7", namespace="ns", hydrate_turtle=False)
        assert item["status"] == "accept_failed"
        assert item["accept_error"]


class TestSweepStaleRecord:
    """The one implementation behind all three stale sweeps (async jobs,
    proposal accepts, induction jobs), which used to be three byte-identical
    inline copies.

    Covers the parameterisation and the best-effort write contract. The write
    guard is new for the async-job and induction-job callers: previously a
    transient DDB failure escaped and 500'd the very poll the user needed to
    observe the stuck state.
    """

    def _stale_item(self, status="running", age=9999):
        from datetime import UTC, datetime, timedelta

        return {
            "status": status,
            "updated_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
        }

    def test_sweeps_and_mutates_item_in_place(self):
        from coa_ontology import dynamo_store as ds

        written = {}
        item = self._stale_item()
        out = ds.sweep_stale_record(
            item,
            is_in_progress=lambda s: s == "running",
            max_age_seconds=60,
            terminal_status="failed",
            error_field="error",
            error_message="worker died",
            write=written.update,
            label="Job x",
        )
        assert out is item  # mutated in place so callers can return it directly
        assert item["status"] == "failed"
        assert item["error"] == "worker died"
        assert written == {"status": "failed", "error": "worker died"}

    def test_extra_fields_are_written_AND_mirrored_onto_the_returned_item(self):
        """``extra_fields`` must land in DDB *and* on the item handed back.

        The caller is typically the very poll that triggered the sweep, so an item
        that carries the new status but the PRE-sweep ``accept_progress`` reports a
        state DDB no longer holds. This asserted only the write before, which is
        how that divergence went unnoticed.
        """
        from coa_ontology import dynamo_store as ds

        written = {}
        item = self._stale_item()
        item["accept_progress"] = {"step": "s3_persist", "state": "done"}  # pre-sweep value
        out = ds.sweep_stale_record(
            item,
            is_in_progress=lambda s: s == "running",
            max_age_seconds=60,
            terminal_status="accept_failed",
            error_field="accept_error",
            error_message="boom",
            write=written.update,
            label="Proposal p",
            extra_fields={"accept_progress": {"step": "unknown", "state": "stale"}},
        )
        assert written["accept_progress"] == {"step": "unknown", "state": "stale"}
        assert out["accept_progress"] == {"step": "unknown", "state": "stale"}
        assert out["status"] == "accept_failed"
        assert out["accept_error"] == "boom"

    def test_not_in_progress_is_left_alone(self):
        from coa_ontology import dynamo_store as ds

        item = self._stale_item(status="completed")
        called = []
        ds.sweep_stale_record(
            item,
            is_in_progress=lambda s: s == "running",
            max_age_seconds=60,
            terminal_status="failed",
            error_field="error",
            error_message="x",
            write=lambda f: called.append(f),
            label="Job x",
        )
        assert item["status"] == "completed"
        assert called == []  # no write for a terminal record, however old

    def test_fresh_record_is_left_alone(self):
        from coa_ontology import dynamo_store as ds

        item = self._stale_item(age=1)
        ds.sweep_stale_record(
            item,
            is_in_progress=lambda s: s == "running",
            max_age_seconds=3600,
            terminal_status="failed",
            error_field="error",
            error_message="x",
            write=lambda f: (_ for _ in ()).throw(AssertionError("must not write")),
            label="Job x",
        )
        assert item["status"] == "running"

    def test_unparseable_and_missing_timestamps_are_left_alone(self):
        from coa_ontology import dynamo_store as ds

        for item in ({"status": "running"}, {"status": "running", "updated_at": "not-a-date"}):
            ds.sweep_stale_record(
                item,
                is_in_progress=lambda s: s == "running",
                max_age_seconds=1,
                terminal_status="failed",
                error_field="error",
                error_message="x",
                write=lambda f: (_ for _ in ()).throw(AssertionError("must not write")),
                label="Job x",
            )
            assert item["status"] == "running"

    def test_write_failure_is_swallowed_and_item_left_unchanged(self):
        """Best-effort write: a transient DDB failure must not propagate (it would
        500 the poll), and the returned item must NOT claim the sweep happened."""
        from coa_ontology import dynamo_store as ds

        item = self._stale_item()
        out = ds.sweep_stale_record(
            item,
            is_in_progress=lambda s: s == "running",
            max_age_seconds=60,
            terminal_status="failed",
            error_field="error",
            error_message="x",
            write=lambda f: (_ for _ in ()).throw(RuntimeError("ddb throttled")),
            label="Job x",
        )
        assert out["status"] == "running"  # not falsely reported as swept
        assert "error" not in out
