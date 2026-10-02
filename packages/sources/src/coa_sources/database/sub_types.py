# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sub-type groupings the create, delete and scan paths branch on.

The sets are deliberately *positive* memberships, so a branch that has not been
considered for a new sub-type fails closed. A ``!= X`` guard admits every future
sub-type by default, which is how delete's federated teardown came to run a Glue
teardown against a Lambda-backed catalog and report success while leaking the
registration.

An ABSENT ``sourceSubType`` is a live row shape — ``sub_type`` is read as
``item.get("sourceSubType", "")``, and rows predating the attribute exist — so ``""``
satisfied every old exclusion and satisfies no new membership. What ``""`` must mean
is NOT the same for all three sets:

* :data:`FEDERATED_TEARDOWN_SUB_TYPES` — ``""`` MUST take this arm, and the guard
  tests it separately (``sub_type in ... or not sub_type``). A legacy JDBC row whose
  federation step provisioned a Glue catalog and connection has those stored names
  matching the derived one, and this teardown is the only thing that removes them.
* :data:`CONNECTOR_BACKED_SUB_TYPES` — ``""`` must NOT be in scope. Both members were
  introduced after ``sourceSubType`` became mandatory, so no attribute-less row can be
  one, and treating ``""`` as connector-backed would point a legacy row's delete at
  ``athena:DeleteDataCatalog`` and at the parameter and tag-verification steps only a
  Databricks source has.
* :data:`PLATFORM_CATALOG_CLAIM_SUB_TYPES` — ``""`` must NOT be in scope. A row
  without a sub-type never claimed a name, so there is none to release.
"""

from __future__ import annotations

from coa_common.constants import CONNECTOR_BACKED_SUB_TYPES
from coa_control_plane_server.models.source_sub_type import SourceSubType

# ``CONNECTOR_BACKED_SUB_TYPES`` lives in ``coa_common.constants`` because serve and
# ontology-engine branch on the same concept and cannot import the generated
# control-plane models; ``test_sub_types.py`` checks it against the generated enum. Its
# members' Athena catalog is a top-level ``LAMBDA``-type catalog bound to a connector
# function, which means:
#   * discovery reads them through Athena SQL, so the metadata-connector registry maps
#     both to ``CustomConnector``;
#   * the federation step has nothing to provision and only flips ``queryable``;
#   * teardown is a plain ``athena:DeleteDataCatalog``, NOT the Lake-Formation-admin
#     path in ``cleanup_federated_resources``;
#   * serve's crawled-name rewrite must NOT apply. It strips a ``{schema}_`` prefix a
#     Glue crawler added, so applying it here turns ``sales_orders`` in schema ``sales``
#     into ``orders`` — a table the connector has never heard of.
__all__ = [
    "CONNECTOR_BACKED_SUB_TYPES",
    "FEDERATED_TEARDOWN_SUB_TYPES",
    "PLATFORM_CATALOG_CLAIM_SUB_TYPES",
]

# Sub-types whose delete tears resources down through the federation provisioner's
# Lake Formation admin role (``glue.delete_catalog``, ``lf.deregister_resource``,
# ``glue.delete_connection``).
#
# ``GLUE_DATABASE`` is in the set not because a native Glue source is federated, but
# because a legacy row can carry a provisioned
# ``glueConnectionName``/``athenaDataCatalogName`` that this teardown is the only thing
# to remove.
#
# An absent ``sourceSubType`` also belongs on this arm, and the guard tests for it
# separately rather than putting ``""`` in the set, which would make ``""`` a recognised
# value everywhere the set is used.
FEDERATED_TEARDOWN_SUB_TYPES: frozenset[str] = frozenset(
    {
        SourceSubType.GLUE_DATABASE.value,
        SourceSubType.JDBC_DATABASE.value,
    }
)

# Sub-types whose create derives a platform catalog name and claims it in the ownership
# record, and whose delete must therefore release that claim. Must stay in step with the
# create path, which claims for every sub-type that derives a name.
PLATFORM_CATALOG_CLAIM_SUB_TYPES: frozenset[str] = frozenset(
    {
        SourceSubType.JDBC_DATABASE.value,
        SourceSubType.CUSTOM_CONNECTOR.value,
        SourceSubType.DATABRICKS_SQL_WAREHOUSE.value,
    }
)
