# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Constraint config is served out-of-band from GET /proposals/{id}.

The proposal detail response must NOT inline ``constraint_config`` when it has
been offloaded to S3: it is the full column/table constraint specification and
can exceed the 6 MB API Gateway / Lambda response cap on wide schemas (thousands
of tables × per-column constraints). Instead the route returns a presigned
``constraints_url`` and the client fetches the config directly from S3.

These guard:
  - ``presign_proposal_constraints`` returns a URL when the S3 object exists and
    ``None`` when it does not (legacy / no-constraints proposals),
  - ``get_proposal_by_id(hydrate_constraints=False)`` skips the constraint S3
    read that the default (server-side consumers) still performs,
  - the ``get_proposal`` route strips inline ``constraint_config`` and adds
    ``constraints_url`` when an S3 object exists, but leaves a legacy inline
    value in place (with a ``None`` url) when there is no S3 object,
  - a wide-schema proposal with large ``constraint_config`` produces a response
    well under the 6 MB API Gateway cap when served out-of-band.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _mock_s3(existing_keys: set[str]):
    """A boto3-shaped S3 mock whose ``head_object`` 404s for absent keys.

    ``generate_presigned_url`` returns a deterministic fake URL so tests can
    assert the key it was signed for.
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


class TestPresignProposalConstraints:
    def test_returns_url_when_object_exists(self):
        from coa_ontology import dynamo_store as ds

        key = ds._proposal_s3_key("ns", "p-1", "constraints")
        with patch("coa_ontology.dynamo_store._get_s3", return_value=_mock_s3({key})):
            url = ds.presign_proposal_constraints("ns", "p-1")
        assert url is not None
        assert key in url

    def test_returns_none_when_object_absent(self):
        from coa_ontology import dynamo_store as ds

        with patch("coa_ontology.dynamo_store._get_s3", return_value=_mock_s3(set())):
            assert ds.presign_proposal_constraints("ns", "p-missing") is None

    def test_non_404_error_propagates(self):
        from botocore.exceptions import ClientError
        from coa_ontology import dynamo_store as ds

        s3 = MagicMock()
        s3.head_object = MagicMock(side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject"))
        with (
            patch("coa_ontology.dynamo_store._get_s3", return_value=s3),
            pytest.raises(ClientError),
        ):
            ds.presign_proposal_constraints("ns", "p-denied")


class TestHydrateConstraintsFlag:
    """``get_proposal_by_id`` reads constraint_config from S3 by default
    (server-side consumers need it), but the detail route opts out so it can
    serve constraints out-of-band via a presigned URL instead."""

    def _item(self):
        return {
            "PK": "ns#PROPOSAL#p-1",
            "SK": "META",
            "proposal_id": "p-1",
            "status": "pending",
            "metadata": {"has_constraint_config": True},
        }

    def _patched(self, s3, table_item):
        table = MagicMock()
        table.get_item.return_value = {"Item": table_item}
        return (
            patch("coa_ontology.dynamo_store._get_table", return_value=table),
            patch("coa_ontology.dynamo_store._get_s3", return_value=s3),
            patch("coa_ontology.dynamo_store._sweep_proposal_if_stale", side_effect=lambda pid, ns, item: item),
        )

    def test_default_hydrates_constraints_from_s3(self):
        from coa_ontology import dynamo_store as ds

        s3 = MagicMock()
        body = MagicMock()
        constraint_data = {"tables": {"orders": {"include": True}}}
        body.read.return_value = json.dumps(constraint_data).encode("utf-8")
        s3.get_object.return_value = {"Body": body}
        # NoSuchKey exception class for the S3 mock
        s3.exceptions = MagicMock()
        s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        p_table, p_s3, p_sweep = self._patched(s3, self._item())
        with p_table, p_s3, p_sweep:
            item = ds.get_proposal_by_id("p-1", namespace="ns")
        assert item["metadata"]["constraint_config"] == constraint_data

    def test_hydrate_constraints_false_skips_s3_read(self):
        from coa_ontology import dynamo_store as ds

        s3 = MagicMock()
        s3.exceptions = MagicMock()
        s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        p_table, p_s3, p_sweep = self._patched(s3, self._item())
        with p_table, p_s3, p_sweep:
            item = ds.get_proposal_by_id("p-1", namespace="ns", hydrate_constraints=False)
        # No S3 read for constraints — the route will presign instead.
        assert "constraint_config" not in (item.get("metadata") or {})

    def test_no_regression_existing_callers_default_true(self):
        """Existing callers that do not pass hydrate_constraints still get the
        constraint_config hydrated (default True preserves backwards compat)."""
        from coa_ontology import dynamo_store as ds

        s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = b'{"columns": {"id": "integer"}}'
        s3.get_object.return_value = {"Body": body}
        s3.exceptions = MagicMock()
        s3.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        p_table, p_s3, p_sweep = self._patched(s3, self._item())
        with p_table, p_s3, p_sweep:
            # Call without hydrate_constraints kwarg — must behave as True.
            item = ds.get_proposal_by_id("p-1", namespace="ns")
        assert "constraint_config" in item["metadata"]


class TestGetProposalRouteConstraints:
    """The GET /proposals/{id} route: strip inline constraint_config + add
    constraints_url when an S3 object exists; keep a legacy inline value (url
    None) when it does not."""

    def _patch_route(self, monkeypatch, *, item, constraints_url, matches_url=None):
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
            lambda namespace, pid: constraints_url,
        )
        return proposals

    def test_modern_proposal_serves_constraints_url_and_strips_inline(self, monkeypatch):
        item = {
            "proposal_id": "p-1",
            "status": "pending",
            "metadata": {
                "has_constraint_config": True,
                "constraint_config": {"tables": {"orders": {"include": True}}},
                "label": "L",
            },
        }
        proposals = self._patch_route(
            monkeypatch, item=item, constraints_url="https://s3.test.local/b/p-1/constraints?sig=x"
        )
        resp = proposals.get_proposal("p-1", namespace="ns")
        assert resp["constraints_url"] == "https://s3.test.local/b/p-1/constraints?sig=x"
        assert "constraint_config" not in resp["metadata"]
        assert resp["metadata"]["label"] == "L"

    def test_legacy_inline_proposal_keeps_inline_and_null_url(self, monkeypatch):
        legacy_config = {"tables": {"orders": {"include": True}}}
        item = {
            "proposal_id": "p-old",
            "status": "pending",
            "metadata": {"constraint_config": legacy_config, "label": "L"},
        }
        proposals = self._patch_route(monkeypatch, item=item, constraints_url=None)
        resp = proposals.get_proposal("p-old", namespace="ns")
        assert resp["constraints_url"] is None
        assert resp["metadata"]["constraint_config"] == legacy_config

    def test_wide_schema_response_under_6mb_cap(self, monkeypatch):
        """A proposal with a large constraint_config must produce a response well
        under the 6 MB API Gateway cap when served out-of-band (the constraint is
        replaced by a short URL string)."""
        # Build a constraint_config that would be ~7 MB inline.
        big_config = {
            f"table_{i}": {f"col_{j}": {"type": "varchar", "nullable": True, "max_length": 255} for j in range(50)}
            for i in range(2000)
        }
        item = {
            "proposal_id": "p-wide",
            "status": "pending",
            "metadata": {
                "has_constraint_config": True,
                "constraint_config": big_config,
                "label": "L",
            },
        }
        proposals = self._patch_route(
            monkeypatch, item=item, constraints_url="https://s3.test.local/b/p-wide/constraints?sig=x"
        )
        resp = proposals.get_proposal("p-wide", namespace="ns")
        serialized_size = len(json.dumps(resp))
        # Must be well under 6 MB — without the presign fix this would be ~7 MB.
        assert serialized_size < 1_000_000, f"Response {serialized_size} bytes exceeds 1 MB safety margin"
        assert resp["constraints_url"] is not None
        assert "constraint_config" not in resp["metadata"]
