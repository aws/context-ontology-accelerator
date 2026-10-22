# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The ontology engine's paginated list routes: the Smithy pagination contract, end to end.

ListProposals, ListInductionJobs, ListOntologies and ListInducedDatasources
declare ``nextToken`` + ``maxResults`` inputs and a wrapped
``{<items>: [...], nextToken}`` output. Before this, the proposals and jobs routes
returned a bare array and ignored ``nextToken``, and ListProposals ignored
``maxResults`` (it bound only ``limit``) — so a caller following the contract
could never see past the first page.

Covered here: the opaque token codec (strict decode — a bad token is a 400, never
a 500, and a token cannot be replayed against another namespace or listing), the
read-page resume rule (no item skipped or repeated across pages, including when a
page ends mid-DynamoDB-page or exactly on a page whose ``LastEvaluatedKey`` is a
filtered-out row), the per-request read budget (short or empty pages that still
carry a usable token), and the routes over HTTP.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from coa_ontology import dynamo_store as ds
from coa_ontology import induce_catalog, pagination, proposals
from coa_ontology.main import app
from coa_ontology.pagination import (
    MAX_TOKEN_LENGTH,
    InvalidNextToken,
    decode_next_token,
    encode_next_token,
    page_body,
)
from fastapi.testclient import TestClient

from .test_dynamo_store import _FakeDynamoTable, _seed_pre_delta_proposal

pytestmark = pytest.mark.unit


def _proposal_page(limit, start):
    """One read-budgeted page of the proposal scan (the shared page reader)."""
    kwargs = ds._proposal_scan_kwargs("ns", None, None, None, None)
    return ds._scan_page(kwargs, limit, start, max_calls=ds.MAX_SCAN_CALLS_PER_PAGE)


_COMMON_SMITHY = Path(__file__).resolve().parents[4] / "models" / "src" / "main" / "smithy" / "common.smithy"


def _raw_token(obj: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


@pytest.fixture()
def client():
    return TestClient(app)


# ── Token codec ──────────────────────────────────────────────────────────────


class TestTokenCodec:
    def test_round_trip(self):
        key = {"PK": "ns#PROPOSAL#p1", "SK": "META"}
        token = encode_next_token(key, scope="proposals:ns")
        assert token and "=" not in token
        assert decode_next_token(token, scope="proposals:ns") == key

    def test_key_outside_the_namespace_round_trips_within_its_scope(self):
        """A budget-stop key may be another namespace's row; the scope, not the PK, binds it."""
        key = {"PK": "other#PROPOSAL#x", "SK": "META"}
        assert decode_next_token(encode_next_token(key, scope="proposals:ns"), scope="proposals:ns") == key

    def test_no_key_means_no_token(self):
        assert encode_next_token(None, scope="s") is None
        assert decode_next_token(None, scope="s") is None
        assert decode_next_token("", scope="s") is None

    @pytest.mark.parametrize(
        "token",
        [
            "!!!not-base64!!!",
            _raw_token("a string"),
            _raw_token(["PK", "SK"]),
            _raw_token({"PK": "ns#PROPOSAL#p1", "SK": "META"}),  # no scope
            _raw_token({"PK": "ns#PROPOSAL#p1", "SK": "META", "scope": "proposals:ns", "extra": "x"}),
            _raw_token({"PK": "ns#PROPOSAL#p1", "SK": 1, "scope": "proposals:ns"}),
            _raw_token({"PK": "", "SK": "META", "scope": "proposals:ns"}),
            base64.urlsafe_b64encode(b"\xff\xfe").decode(),
            "A" * (MAX_TOKEN_LENGTH + 1),
        ],
    )
    def test_malformed_token_is_rejected(self, token):
        with pytest.raises(InvalidNextToken):
            decode_next_token(token, scope="proposals:ns")

    def test_token_from_another_scope_is_rejected(self):
        token = encode_next_token({"PK": "ns#PROPOSAL#p1", "SK": "META"}, scope="proposals:other")
        with pytest.raises(InvalidNextToken):
            decode_next_token(token, scope="proposals:ns")

    def test_pk_pin_rejects_another_partition(self):
        token = encode_next_token({"PK": "NAMESPACE#b", "SK": "ONTOLOGY#x"}, scope="ontologies:a")
        with pytest.raises(InvalidNextToken):
            decode_next_token(token, scope="ontologies:a", pk="NAMESPACE#a")

    def test_max_ontology_id_key_fits_the_bound(self):
        """A registry key embeds an ontology id of up to 2048 characters."""
        key = {"PK": "NAMESPACE#" + "a" * 36, "SK": "ONTOLOGY#" + "x" * 2048}
        token = encode_next_token(key, scope="ontologies:" + "a" * 36)
        assert token is not None
        assert len(token) <= MAX_TOKEN_LENGTH

    def test_bound_does_not_exceed_the_smithy_pagination_token_max(self):
        text = _COMMON_SMITHY.read_text()
        m = re.search(r"@length\(min:\s*\d+,\s*max:\s*(\d+)\)\s*string PaginationToken", text)
        assert m, "PaginationToken @length not found in common.smithy"
        assert int(m.group(1)) >= MAX_TOKEN_LENGTH

    def test_page_body_omits_the_token_on_the_last_page(self):
        assert page_body("jobs", [1], None, scope="s") == {"jobs": [1]}
        body = page_body("jobs", [1], {"PK": "a", "SK": "b"}, scope="s")
        assert decode_next_token(body["nextToken"], scope="s") == {"PK": "a", "SK": "b"}


# ── Read paging ──────────────────────────────────────────────────────────────


def _seed_proposals(table: _FakeDynamoTable, ns: str, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        _seed_pre_delta_proposal(table, namespace=ns, proposal_id=f"p{i}")


def _use(monkeypatch, table: _FakeDynamoTable) -> _FakeDynamoTable:
    monkeypatch.setattr(ds, "_get_table", lambda: table)
    return table


class TestProposalPages:
    @pytest.mark.parametrize(("page_size", "scan_page"), [(2, None), (2, 3), (3, 2), (5, 5), (1, 1)])
    def test_pages_cover_every_item_exactly_once(self, monkeypatch, page_size, scan_page):
        """Walk every page: the union is all items, with no repeats.

        ``scan_page`` (the fake's intrinsic DynamoDB page size) not dividing
        ``page_size`` is the case that matters — a page then ends mid-scan-page,
        and resuming from the scan's own LastEvaluatedKey would skip the rest.
        """
        table = _use(monkeypatch, _FakeDynamoTable(page_size=scan_page))
        _seed_proposals(table, "ns", 7)
        _seed_proposals(table, "other", 3)  # interleaved noise from another namespace

        seen: list[str] = []
        start = None
        for _ in range(20):
            items, start = _proposal_page(page_size, start)
            assert len(items) <= page_size
            seen.extend(i["proposal_id"] for i in items)
            if start is None:
                break
        assert sorted(seen) == sorted(f"p{i}" for i in range(7))
        assert len(seen) == len(set(seen))

    def test_exact_fill_resumes_from_the_last_returned_item(self, monkeypatch):
        """The page fills exactly and the scan's LastEvaluatedKey is another namespace's row.

        That key used to be returned as the resume point; it failed the token's
        namespace check, so the caller's next request was a 400.
        """
        table = _use(monkeypatch, _FakeDynamoTable(page_size=3))
        _seed_proposals(table, "ns", 2)  # p0, p1
        _seed_pre_delta_proposal(table, namespace="other", proposal_id="x0")  # LastEvaluatedKey of page 1
        _seed_proposals(table, "ns", 1, start=2)  # p2

        items, next_key = _proposal_page(2, None)

        assert [i["proposal_id"] for i in items] == ["p0", "p1"]
        assert next_key == {"PK": "ns#PROPOSAL#p1", "SK": "META"}
        rest, last = _proposal_page(2, next_key)
        assert [i["proposal_id"] for i in rest] == ["p2"]
        assert last is None

    def test_read_budget_returns_a_short_page_with_a_resume_key(self, monkeypatch):
        """Few matches in a large table: one request stops after the budget, not at the end."""
        table = _use(monkeypatch, _FakeDynamoTable(page_size=1))
        _seed_proposals(table, "other", ds.MAX_SCAN_CALLS_PER_PAGE + 5)
        _seed_proposals(table, "ns", 1)

        items, next_key = _proposal_page(5, None)

        assert items == []
        assert len(table.scan_calls) == ds.MAX_SCAN_CALLS_PER_PAGE
        assert next_key is not None and next_key["PK"].startswith("other#")
        rest, last = _proposal_page(5, next_key)
        assert [i["proposal_id"] for i in rest] == ["p0"]
        assert last is None

    def test_last_page_has_no_next_key(self, monkeypatch):
        table = _use(monkeypatch, _FakeDynamoTable())
        _seed_proposals(table, "ns", 2)

        items, next_key = _proposal_page(5, None)
        assert len(items) == 2
        assert next_key is None

    def test_list_proposals_with_limit_still_returns_a_plain_list(self, monkeypatch):
        """The internal callers' helper is unchanged by the paged variant."""
        table = _use(monkeypatch, _FakeDynamoTable(page_size=2))
        _seed_proposals(table, "ns", 5)

        assert len(ds.list_proposals(namespace="ns", limit=3)) == 3
        assert ds.list_proposals(namespace="ns", limit=0) == []

    def test_internal_drain_is_not_budgeted(self, monkeypatch):
        """``limit=None`` callers (cleanup paths) must see every match, however long the scan."""
        table = _use(monkeypatch, _FakeDynamoTable(page_size=1))
        _seed_proposals(table, "other", ds.MAX_SCAN_CALLS_PER_PAGE + 5)
        _seed_proposals(table, "ns", 2)

        assert len(ds.list_proposals(namespace="ns", limit=None)) == 2


class TestJobPages:
    def test_scan_calls_carry_no_per_call_limit(self, monkeypatch):
        """``Limit`` caps rows evaluated, so ``Limit=maxResults`` meant table_size/maxResults round trips."""
        table = _use(monkeypatch, _FakeDynamoTable())
        ds.list_jobs_page(namespace="ns", limit=5)
        assert table.scan_calls and all("Limit" not in c for c in table.scan_calls)


# ── Routes ───────────────────────────────────────────────────────────────────


class TestListProposalsRoute:
    @pytest.fixture()
    def table(self, monkeypatch):
        table = _use(monkeypatch, _FakeDynamoTable(page_size=2))
        _seed_proposals(table, "ns", 5)
        return table

    def test_response_is_the_wrapped_contract_shape(self, client, table):
        resp = client.get("/ontology/proposals/?namespace=ns&maxResults=10")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"proposals"}  # no nextToken on the last page
        assert len(body["proposals"]) == 5
        assert all("ontology_turtle" not in p for p in body["proposals"])

    def test_max_results_and_next_token_page_through_everything(self, client, table):
        seen: list[str] = []
        url = "/ontology/proposals/?namespace=ns&maxResults=2"
        token = None
        for _ in range(10):
            resp = client.get(url + (f"&nextToken={token}" if token else ""))
            assert resp.status_code == 200
            body = resp.json()
            assert len(body["proposals"]) <= 2
            seen.extend(p["proposal_id"] for p in body["proposals"])
            token = body.get("nextToken")
            if not token:
                break
        assert sorted(seen) == [f"p{i}" for i in range(5)]

    def test_foreign_rows_between_pages_keep_the_token_usable(self, client, monkeypatch):
        table = _use(monkeypatch, _FakeDynamoTable(page_size=3))
        _seed_proposals(table, "ns", 2)
        _seed_pre_delta_proposal(table, namespace="other", proposal_id="x0")
        _seed_proposals(table, "ns", 1, start=2)

        first = client.get("/ontology/proposals/?namespace=ns&maxResults=2").json()
        second = client.get(f"/ontology/proposals/?namespace=ns&maxResults=2&nextToken={first['nextToken']}")

        assert second.status_code == 200
        ids = [p["proposal_id"] for p in first["proposals"] + second.json()["proposals"]]
        assert sorted(ids) == ["p0", "p1", "p2"] and len(ids) == 3
        assert "nextToken" not in second.json()

    def test_many_foreign_rows_do_not_push_a_namespace_off_the_first_page(self, client, monkeypatch):
        table = _use(monkeypatch, _FakeDynamoTable(page_size=1))
        _seed_proposals(table, "other", ds.MAX_SCAN_CALLS_PER_PAGE + 1)
        _seed_proposals(table, "ns", 1)

        first = client.get("/ontology/proposals/?namespace=ns").json()
        assert [p["proposal_id"] for p in first["proposals"]] == ["p0"]
        assert "nextToken" not in first

    def test_legacy_limit_still_binds(self, client, table):
        resp = client.get("/ontology/proposals/?namespace=ns&limit=1")
        assert len(resp.json()["proposals"]) == 1
        assert "nextToken" in resp.json()

    def test_max_results_wins_over_limit(self, client, table):
        resp = client.get("/ontology/proposals/?namespace=ns&limit=1&maxResults=3")
        assert len(resp.json()["proposals"]) == 3

    @pytest.mark.parametrize(
        ("query", "expected"),
        [("ontologyId=urn:o", "urn:o"), ("ontology_id=urn:legacy", "urn:legacy"), ("ontologyId=a&ontology_id=b", "a")],
    )
    def test_ontology_id_spellings_bind(self, client, query, expected):
        with patch.object(proposals.dynamo_store, "list_proposals_newest_page", return_value=([], None)) as m:
            client.get(f"/ontology/proposals/?namespace=ns&{query}")
        assert m.call_args.kwargs["ontology_id"] == expected

    @pytest.mark.parametrize("max_results", ["0", str(proposals.LIST_PROPOSALS_MAX_RESULTS + 1), "x"])
    def test_out_of_range_max_results_is_rejected(self, client, max_results):
        resp = client.get(f"/ontology/proposals/?namespace=ns&maxResults={max_results}")
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "token",
        [
            "garbage",
            _raw_token({"PK": "other#PROPOSAL#p1", "SK": "META", "scope": "proposals:other"}),
            _raw_token({"a": 1}),
        ],
    )
    def test_malformed_or_foreign_token_is_400(self, client, table, token):
        resp = client.get(f"/ontology/proposals/?namespace=ns&nextToken={token}")
        assert resp.status_code == 400
        assert token not in resp.text


class TestListJobsRoute:
    @pytest.fixture()
    def table(self, monkeypatch):
        table = _use(monkeypatch, _FakeDynamoTable(page_size=2))
        for i in range(5):
            table.put_item(
                Item={
                    "PK": f"ns#JOB#j{i}",
                    "SK": "STATUS",
                    "job_id": f"j{i}",
                    "status": "completed",
                    "created_at": f"2026-01-0{i + 1}T00:00:00Z",
                }
            )
        table.put_item(Item={"PK": "ns#JOB#j0", "SK": "PROPOSAL#ontology"})  # non-status row
        return table

    def test_jobs_page_through_everything(self, client, table):
        seen: list[str] = []
        token = None
        for _ in range(10):
            resp = client.get("/induce/jobs?namespace=ns&maxResults=2" + (f"&nextToken={token}" if token else ""))
            assert resp.status_code == 200
            body = resp.json()
            assert len(body["jobs"]) <= 2
            seen.extend(j["job_id"] for j in body["jobs"])
            token = body.get("nextToken")
            if not token:
                break
        assert sorted(seen) == [f"j{i}" for i in range(5)]

    def test_token_from_proposals_listing_is_rejected(self, client, table):
        token = encode_next_token({"PK": "ns#PROPOSAL#p1", "SK": "META"}, scope=proposals.proposals_list_scope("ns"))
        resp = client.get(f"/induce/jobs?namespace=ns&nextToken={token}")
        assert resp.status_code == 400

    @pytest.mark.parametrize("next_token", [None, "resume"])
    def test_scan_failure_is_503_not_an_empty_last_page(self, client, next_token):
        """An empty 200 without ``nextToken`` reads as "done" and silently truncates a paging client."""
        token = (
            encode_next_token({"PK": "ns#JOB#j1", "SK": "STATUS"}, scope=induce_catalog.jobs_list_scope("ns"))
            if next_token
            else None
        )
        with patch.object(
            induce_catalog.dynamo_store, "list_jobs_page", side_effect=RuntimeError("arn:aws:dynamodb:secret")
        ):
            resp = client.get("/induce/jobs?namespace=ns" + (f"&nextToken={token}" if token else ""))
        assert resp.status_code == 503
        assert "arn:aws" not in resp.text


class TestListOntologiesRoute:
    @pytest.fixture()
    def table(self, monkeypatch):
        table = _use(monkeypatch, _FakeDynamoTable(page_size=2))
        for i in range(5):
            table.put_item(
                Item={
                    "PK": "NAMESPACE#ns",
                    "SK": f"ONTOLOGY#urn:o{i}",
                    "ontology_id": f"urn:o{i}",
                    "uri": f"urn:o{i}",
                    "ontology_type": "induced" if i % 2 else "foundational",
                    "license_tier": "restricted" if i == 3 else "open",
                }
            )
        table.put_item(Item={"PK": "NAMESPACE#other", "SK": "ONTOLOGY#urn:x", "ontology_id": "urn:x", "uri": "urn:x"})
        return table

    def _ids(self, body: dict) -> list[str]:
        return [o["ontologyId"] for o in body["ontologies"]]

    def test_max_results_and_next_token_page_through_everything(self, client, table):
        seen: list[str] = []
        token = None
        for _ in range(10):
            resp = client.get("/ontologies/?namespace=ns&maxResults=2" + (f"&nextToken={token}" if token else ""))
            assert resp.status_code == 200
            body = resp.json()
            seen.extend(self._ids(body))
            token = body.get("nextToken")
            if not token:
                break
        assert sorted(seen) == [f"urn:o{i}" for i in range(5)]

    def test_legacy_skip_and_limit_still_bind(self, client, table):
        body = client.get("/ontologies/?namespace=ns&skip=1&limit=2").json()
        assert self._ids(body) == ["urn:o1", "urn:o2"]
        rest = client.get(f"/ontologies/?namespace=ns&maxResults=10&nextToken={body['nextToken']}").json()
        assert self._ids(rest) == ["urn:o3", "urn:o4"]

    def test_extra_filters_apply_before_paging(self, client, table):
        """``license_tier`` used to be applied after the page was cut, thinning it."""
        body = client.get("/ontologies/?namespace=ns&license_tier=restricted&maxResults=1").json()
        assert self._ids(body) == ["urn:o3"]
        assert self._ids(client.get("/ontologies/?namespace=ns&uri=urn:o4").json()) == ["urn:o4"]

    def test_token_for_another_namespace_is_400(self, client, table):
        token = encode_next_token({"PK": "NAMESPACE#other", "SK": "ONTOLOGY#urn:x"}, scope="ontologies:other")
        assert client.get(f"/ontologies/?namespace=ns&nextToken={token}").status_code == 400

    @pytest.mark.parametrize("query", ["maxResults=0", "maxResults=1001", "limit=1001", "skip=-1", "skip=10001"])
    def test_out_of_range_page_parameters_are_rejected(self, client, table, query):
        assert client.get(f"/ontologies/?namespace=ns&{query}").status_code == 422


class TestListInducedDatasourcesRoute:
    def _accepted(self, *ds_ids: str) -> list[dict]:
        return [{"proposal_id": f"p-{d}", "ontology_id": "urn:o", "metadata": {"datasource_ids": [d]}} for d in ds_ids]

    def test_pages_in_datasource_id_order(self, client):
        with patch.object(induce_catalog.dynamo_store, "list_proposals", return_value=self._accepted("c", "a", "b")):
            first = client.get("/induce/datasources/induced?namespace=ns&maxResults=2").json()
            second = client.get(
                f"/induce/datasources/induced?namespace=ns&maxResults=2&nextToken={first['nextToken']}"
            ).json()
        assert [d["datasourceId"] for d in first["datasources"]] == ["a", "b"]
        assert [d["datasourceId"] for d in second["datasources"]] == ["c"]
        assert "nextToken" not in second

    def test_reads_every_accepted_proposal(self, client):
        with patch.object(induce_catalog.dynamo_store, "list_proposals", return_value=[]) as m:
            assert client.get("/induce/datasources/induced?namespace=ns").json() == {"datasources": []}
        m.assert_called_once_with(namespace="ns", status="accepted", limit=None)

    def test_token_for_another_namespace_is_400(self, client):
        token = encode_next_token({"PK": "other", "SK": "a"}, scope="induced-datasources:other")
        resp = client.get(f"/induce/datasources/induced?namespace=ns&nextToken={token}")
        assert resp.status_code == 400


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        ("x" * (pagination.MAX_TOKEN_LENGTH + 1), "too_long"),
        ("!!!", "undecodable"),
        (base64.urlsafe_b64encode(b'{"PK":"a"}').decode().rstrip("="), "wrong_shape"),
        (pagination.encode_next_token({"PK": "a", "SK": "b"}, scope="other"), "wrong_scope"),
        (pagination.encode_next_token({"PK": "a", "SK": "b"}, scope="s"), "wrong_partition"),
    ],
)
def test_rejection_reason_is_logged_but_never_returned(token, reason, caplog):
    with (
        caplog.at_level(logging.INFO, logger=pagination.logger.name),
        pytest.raises(pagination.InvalidNextToken) as exc,
    ):
        pagination.decode_next_token(token, scope="s", pk="z")
    assert exc.value.reason == reason
    assert str(exc.value) == "Invalid nextToken."
    assert f"reason={reason}" in caplog.text


class TestListProposalsNewestFirstPages:
    """The ListProposals route pages newest first: limit keeps the newest N, later pages continue older."""

    @staticmethod
    def _rows(n):
        return [
            {
                "PK": f"ns#PROPOSAL#p{i:02d}",
                "SK": "META",
                "proposal_id": f"p{i:02d}",
                "created_at": f"2026-10-{i + 1:02d}T00:00:00Z",
            }
            for i in range(n)
        ]

    def test_pages_are_newest_first_and_cover_every_proposal_once(self):
        rows = self._rows(7)
        with patch.object(ds, "list_proposals", return_value=sorted(rows, key=ds._newest_first_key, reverse=True)):
            seen, after = [], None
            for _ in range(10):
                page, after = ds.list_proposals_newest_page(namespace="ns", limit=3, after=after)
                seen += [r["proposal_id"] for r in page]
                if after is None:
                    break
        assert seen == [f"p{i:02d}" for i in range(6, -1, -1)]

    def test_limit_returns_the_newest(self):
        rows = self._rows(5)
        with patch.object(ds, "list_proposals", return_value=sorted(rows, key=ds._newest_first_key, reverse=True)):
            page, after = ds.list_proposals_newest_page(namespace="ns", limit=2)
        assert [r["proposal_id"] for r in page] == ["p04", "p03"]
        assert after == {"PK": "ns#PROPOSAL#p03", "SK": "2026-10-04T00:00:00Z"}

    def test_a_proposal_created_between_pages_does_not_shift_the_rest(self):
        rows = self._rows(4)
        newest = sorted(rows, key=ds._newest_first_key, reverse=True)
        with patch.object(ds, "list_proposals", return_value=newest):
            first, after = ds.list_proposals_newest_page(namespace="ns", limit=2)
        fresh = {"PK": "ns#PROPOSAL#zz", "SK": "META", "proposal_id": "zz", "created_at": "2026-12-01T00:00:00Z"}
        with patch.object(ds, "list_proposals", return_value=[fresh, *newest]):
            second, last = ds.list_proposals_newest_page(namespace="ns", limit=2, after=after)
        assert [r["proposal_id"] for r in first + second] == ["p03", "p02", "p01", "p00"]
        assert last is None

    def test_rows_without_created_at_sort_last(self):
        rows = [{"PK": "ns#PROPOSAL#legacy", "SK": "META", "proposal_id": "legacy"}, *self._rows(2)]
        with patch.object(ds, "list_proposals", return_value=sorted(rows, key=ds._newest_first_key, reverse=True)):
            page, _ = ds.list_proposals_newest_page(namespace="ns", limit=5)
        assert [r["proposal_id"] for r in page] == ["p01", "p00", "legacy"]
