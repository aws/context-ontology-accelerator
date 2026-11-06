# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tripwires on the sub-type groupings the create, delete and scan paths branch on.

Adding a sub-type is one edit in one file; forgetting that edit fails here rather than
failing open at runtime.
"""

from __future__ import annotations

import pytest
from coa_common.constants import DATABASE_SUB_TYPES
from coa_control_plane_server.models.source_sub_type import SourceSubType
from coa_sources.database.sub_types import (
    CONNECTOR_BACKED_SUB_TYPES,
    FEDERATED_TEARDOWN_SUB_TYPES,
    GRAPH_BACKED_SUB_TYPES,
    PLATFORM_CATALOG_CLAIM_SUB_TYPES,
)

pytestmark = pytest.mark.unit

_DOCUMENT_SUB_TYPES = frozenset({SourceSubType.S3.value, SourceSubType.LOCAL_UPLOAD.value})
_DATABASE_SUB_TYPES = frozenset(s.value for s in SourceSubType) - _DOCUMENT_SUB_TYPES


def test_the_shared_mirror_matches_the_generated_enum():
    """Serve's catalog resolution and ontology-engine's datasource list both DERIVE their
    sets from the ``coa_common.constants`` mirror, so this assertion is what keeps them
    complete. A DATABASE member the mirror lacks is silent: the source drops out of
    induction with a bare ``continue``.
    """
    assert DATABASE_SUB_TYPES == _DATABASE_SUB_TYPES, (
        "coa_common.constants.DatabaseSubType is out of sync with the generated SourceSubType enum. "
        "Add the DATABASE member(s) there; document sub-types belong in neither."
    )
    assert CONNECTOR_BACKED_SUB_TYPES <= DATABASE_SUB_TYPES
    assert GRAPH_BACKED_SUB_TYPES <= DATABASE_SUB_TYPES


def test_every_database_sub_type_has_exactly_one_teardown_path():
    """The three teardown paths must between them cover every DATABASE sub-type, or a
    source's catalog is torn down by none. A graph source has no catalog at all, which is
    its own explicit arm rather than an omission.
    """
    covered = CONNECTOR_BACKED_SUB_TYPES | FEDERATED_TEARDOWN_SUB_TYPES | GRAPH_BACKED_SUB_TYPES
    uncovered = _DATABASE_SUB_TYPES - covered
    assert not uncovered, (
        f"DATABASE sub-type(s) {sorted(uncovered)} belong to no teardown path. Add each to "
        f"CONNECTOR_BACKED_SUB_TYPES (a top-level LAMBDA catalog: plain athena:DeleteDataCatalog), to "
        f"FEDERATED_TEARDOWN_SUB_TYPES (the Lake-Formation-admin path) or to GRAPH_BACKED_SUB_TYPES "
        f"(no catalog), and check the delete handler."
    )


def test_the_teardown_paths_do_not_overlap():
    """A sub-type in both catalog paths would have its catalog deleted by
    athena:DeleteDataCatalog and then handed to a Lake-Formation-admin teardown that
    assumes a role scoped to the deployment-wide ``{prefix}ds_*`` window. A graph source
    in either would run a catalog teardown against a catalog that never existed."""
    assert not (CONNECTOR_BACKED_SUB_TYPES & FEDERATED_TEARDOWN_SUB_TYPES)
    assert not (GRAPH_BACKED_SUB_TYPES & (CONNECTOR_BACKED_SUB_TYPES | FEDERATED_TEARDOWN_SUB_TYPES))


def test_a_graph_source_claims_no_platform_catalog():
    assert not (GRAPH_BACKED_SUB_TYPES & PLATFORM_CATALOG_CLAIM_SUB_TYPES)
    assert SourceSubType.RDF_SPARQL.value in GRAPH_BACKED_SUB_TYPES


def test_every_connector_backed_sub_type_claims_a_platform_catalog():
    """Create claims the derived catalog name for every sub-type that derives one, and
    delete must release it or the name stays owned by a source that no longer exists."""
    assert CONNECTOR_BACKED_SUB_TYPES <= PLATFORM_CATALOG_CLAIM_SUB_TYPES


def test_databricks_is_connector_backed_and_claims_a_catalog():
    assert SourceSubType.DATABRICKS_SQL_WAREHOUSE.value in CONNECTOR_BACKED_SUB_TYPES
    assert SourceSubType.DATABRICKS_SQL_WAREHOUSE.value in PLATFORM_CATALOG_CLAIM_SUB_TYPES
    # Emphatically NOT federated: its catalog has no Glue object behind it, so
    # glue.delete_catalog against it succeeds without deleting anything.
    assert SourceSubType.DATABRICKS_SQL_WAREHOUSE.value not in FEDERATED_TEARDOWN_SUB_TYPES


def test_a_native_glue_source_claims_no_platform_catalog():
    """It is given no derived name, so adding it here would make every Glue delete try to
    release a claim that never existed."""
    assert SourceSubType.GLUE_DATABASE.value not in PLATFORM_CATALOG_CLAIM_SUB_TYPES
