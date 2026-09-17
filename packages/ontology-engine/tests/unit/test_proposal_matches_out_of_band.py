# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Grounding matches are served out-of-band from GET /proposals/{id}.

The proposal detail response must NOT inline the grounding-match list: it is one
entry per grounded table, each with up to ~30 candidate classes (with
definitions), so a wide schema can exceed the 6 MB API Gateway / Lambda response
cap — the same reason the (multi-MB) Turtle is already served via a presigned S3
URL. Instead the route returns a presigned ``matches_url`` and the client fetches
the full list directly from S3. This is what makes the ``[:50]`` truncation
removal safe at scale: nothing is dropped, and the response stays small no matter
how many tables grounded.

These guard:
  - ``presign_proposal_matches`` returns a URL when the S3 object exists and
    ``None`` when it does not (all-novel / legacy inline proposals),
  - ``get_proposal_by_id(hydrate_matches=False)`` skips the matches S3 read that
    the default (server-side consumers) still performs,
  - the ``get_proposal`` route strips inline matches and adds ``matches_url``
    when an S3 object exists, but leaves a legacy inline list in place (with a
    ``None`` url) when there is no S3 object.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _mock_s3(existing_keys: set[str]):
    """A boto3-shaped S3 mock whose ``head_object`` 404s for absent keys.

    ``generate_presigned_url`` returns a deterministic fake URL so tests can
    assert the key it was signed for. Mirrors the seam used in
    ``test_induction_e2e``.
    """
    from botocore.exceptions import ClientError

    s3 = MagicMock()

    def fake_head_object(Bucket, Key, **kwargs):
        if Key not in existing_keys:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": 123}

    def fake_generate_presigned_url(ClientMethod, Params=None, ExpiresIn=3600, **kwargs):
        params = Params or {}
        return f"https://s3.test.local/{params.get('Bucket', 'b')}/{params.get('Key', '')}?sig=test"

    s3.head_object = MagicMock(side_effect=fake_head_object)
    s3.generate_presigned_url = MagicMock(side_effect=fake_generate_presigned_url)
    return s3


class TestPresignProposalMatches:
    def test_returns_url_when_object_exists(self):
        from coa_ontology import dynamo_store as ds

        key = ds._proposal_matches_s3_key("ns", "p-1")
        with patch("coa_ontology.dynamo_store._get_s3", return_value=_mock_s3({key})):
            url = ds.presign_proposal_matches("ns", "p-1")
        # A real presigned URL for exactly the matches.json key.
        assert url is not None
        assert key in url

    def test_returns_none_when_object_absent(self):
        from coa_ontology import dynamo_store as ds

        # Empty key set -> head_object 404s -> None (all-novel / legacy inline).
        with patch("coa_ontology.dynamo_store._get_s3", return_value=_mock_s3(set())):
            assert ds.presign_proposal_matches("ns", "p-missing") is None

    def test_non_404_error_propagates(self):
        # A permissions/throttling error is a real failure and must NOT be
        # swallowed into a (misleading) ``None`` "no matches" result.
        from botocore.exceptions import ClientError
        from coa_ontology import dynamo_store as ds

        s3 = MagicMock()
        s3.head_object = MagicMock(side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject"))
        with (
            patch("coa_ontology.dynamo_store._get_s3", return_value=s3),
            pytest.raises(ClientError),
        ):
            ds.presign_proposal_matches("ns", "p-denied")


class TestHydrateMatchesFlag:
    """``get_proposal_by_id`` reads matches from S3 by default (server-side
    consumers such as the grounding-override rebuild need the full list), but the
    detail route opts out so it can serve them out-of-band instead."""

    def _item(self):
        return {
            "PK": "ns#PROPOSAL#p-1",
            "SK": "META",
            "proposal_id": "p-1",
            "status": "pending",
            "matches_s3_key": "proposals/ns/p-1/latest/matches.json",
        }

    def _patched(self, s3, table_item):
        table = MagicMock()
        table.get_item.return_value = {"Item": table_item}
        return (
            patch("coa_ontology.dynamo_store._get_table", return_value=table),
            patch("coa_ontology.dynamo_store._get_s3", return_value=s3),
            # Neutralize the stale-sweep so it doesn't touch the mock table.
            patch("coa_ontology.dynamo_store._sweep_proposal_if_stale", side_effect=lambda pid, ns, item: item),
        )

    def test_default_hydrates_matches_from_s3(self):
        from coa_ontology import dynamo_store as ds

        s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = b'{"matches": [{"source_table": "t", "match_type": "novel"}]}'
        s3.get_object.return_value = {"Body": body}
        p_table, p_s3, p_sweep = self._patched(s3, self._item())
        with p_table, p_s3, p_sweep:
            item = ds.get_proposal_by_id("p-1", namespace="ns")
        # Default path pulled the list into metadata.matches.
        assert item["metadata"]["matches"] == [{"source_table": "t", "match_type": "novel"}]
        s3.get_object.assert_called_once()

    def test_hydrate_matches_false_skips_s3_read(self):
        from coa_ontology import dynamo_store as ds

        s3 = MagicMock()
        p_table, p_s3, p_sweep = self._patched(s3, self._item())
        with p_table, p_s3, p_sweep:
            item = ds.get_proposal_by_id("p-1", namespace="ns", hydrate_matches=False)
        # No S3 read, no inline matches — the route will presign instead.
        s3.get_object.assert_not_called()
        assert "matches" not in (item.get("metadata") or {})


class TestGetProposalRoute:
    """The GET /proposals/{id} route: strip inline matches + add matches_url when
    an S3 object exists; keep a legacy inline list (url None) when it does not."""

    def _patch_route(self, monkeypatch, *, item, matches_url):
        from coa_ontology import proposals

        monkeypatch.setattr(
            proposals.dynamo_store,
            "get_proposal_by_id",
            lambda pid, namespace="default", hydrate_turtle=True, hydrate_matches=True, hydrate_constraints=True: item,
        )
        monkeypatch.setattr(
            proposals.dynamo_store,
            "presign_proposal_artifact",
            lambda namespace, pid, artifact: None,
        )
        monkeypatch.setattr(
            proposals.dynamo_store,
            "presign_proposal_matches",
            lambda namespace, pid: matches_url,
        )
        monkeypatch.setattr(
            proposals.dynamo_store,
            "presign_proposal_constraints",
            lambda namespace, pid: None,
        )
        return proposals

    def test_modern_proposal_serves_matches_url_and_strips_inline(self, monkeypatch):
        # Offloaded proposal: an S3 object exists (presign returns a URL). Even if
        # an inline copy somehow rode along, the route must drop it so the body
        # cannot carry the (potentially multi-MB) list.
        item = {
            "proposal_id": "p-1",
            "status": "pending",
            "matches_s3_key": "proposals/ns/p-1/latest/matches.json",
            "metadata": {"matches": [{"source_table": "leftover"}], "label": "L"},
        }
        proposals = self._patch_route(
            monkeypatch, item=item, matches_url="https://s3.test.local/b/p-1/matches.json?sig=x"
        )
        resp = proposals.get_proposal("p-1", namespace="ns")
        assert resp["matches_url"] == "https://s3.test.local/b/p-1/matches.json?sig=x"
        assert "matches" not in resp["metadata"]
        # Unrelated metadata is untouched.
        assert resp["metadata"]["label"] == "L"

    def test_legacy_inline_proposal_keeps_inline_and_null_url(self, monkeypatch):
        # Pre-offload proposal: no S3 object (presign -> None). The small inline
        # list (formerly [:50]-capped) must survive so the client's
        # inline-else-URL fallback still renders its grounding.
        legacy_matches = [{"source_table": "orders", "match_type": "exact"}]
        item = {
            "proposal_id": "p-old",
            "status": "pending",
            "metadata": {"matches": legacy_matches, "label": "L"},
        }
        proposals = self._patch_route(monkeypatch, item=item, matches_url=None)
        resp = proposals.get_proposal("p-old", namespace="ns")
        assert resp["matches_url"] is None
        assert resp["metadata"]["matches"] == legacy_matches
