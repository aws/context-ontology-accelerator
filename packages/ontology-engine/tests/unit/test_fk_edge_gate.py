# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the FK-edge approval gate (#1088).

`_fk_edge_allowed` decides whether a foreign key is materialised as an
`owl:ObjectProperty`. "Use only approved relationships downstream": inferred FKs
awaiting (or refused) review must NOT become edges, while authoritative and
grandfathered FKs are unaffected so existing ontologies do not regress.
"""

from __future__ import annotations

import pytest
from coa_common.domain_models import EnrichmentSource, ReviewStatus
from coa_ontology.inducer.strategies.table_to_ontology import _fk_edge_allowed

pytestmark = pytest.mark.unit


class TestFkEdgeGate:
    @pytest.mark.parametrize(
        "source",
        [
            EnrichmentSource.DETERMINISTIC,
            EnrichmentSource.STEWARD_SPECIFIED,
            EnrichmentSource.STEWARD_EDITED,
            EnrichmentSource.CATALOG_EXISTING,
        ],
    )
    def test_authoritative_sources_emit_even_when_pending(self, source):
        # Source-system / catalog / steward FKs are authoritative — review state
        # is irrelevant, they always materialise.
        assert _fk_edge_allowed(source, ReviewStatus.PENDING_REVIEW) is True

    @pytest.mark.parametrize("status", ["", None])
    def test_grandfathered_empty_status_emits(self, status):
        # No explicit review state (legacy FK, or a catalog source that does not
        # populate it) is grandfathered so re-induction does not drop the edge.
        assert _fk_edge_allowed(EnrichmentSource.AI_INFERRED, status) is True

    def test_approved_inferred_emits(self):
        assert _fk_edge_allowed(EnrichmentSource.AI_INFERRED, ReviewStatus.APPROVED) is True

    @pytest.mark.parametrize("status", [ReviewStatus.PENDING_REVIEW, ReviewStatus.REJECTED])
    def test_pending_or_rejected_inferred_is_withheld(self, status):
        # The one behaviour change: a NEW inferred FK carrying an explicit
        # non-approved status is withheld from the ontology until approved.
        assert _fk_edge_allowed(EnrichmentSource.AI_INFERRED, status) is False
