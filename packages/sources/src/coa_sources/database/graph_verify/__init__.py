# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Registration-time verification of RDF_SPARQL (graph) sources.

A graph source has no relational schema, so it never enters the DB-scan pipeline.
Registration and re-scan instead enqueue a message on the graph-verify queue; the
consumer (:mod:`.handler`) runs a reachability probe chosen by the source's
``configuration.store`` and flips the record to ``APPROVED``/``queryable`` or
``SCAN_FAILED``. This module holds the message contract both sides share.
"""

from __future__ import annotations

import json

# scanType of the scan-job row a verify writes, distinguishing it in Scan History from
# a SQL discovery scan ("full").
REACHABILITY_SCAN_TYPE = "reachability"


def verify_message(source_id: str, namespace_id: str, scan_job_sk: str) -> str:
    """SQS body asking the verify Lambda to probe one source.

    ``scanJobSK`` names the IN_PROGRESS scan-job row the API wrote, so the verify
    writes its terminal result onto the same Scan History line.
    """
    return json.dumps({"sourceId": source_id, "namespaceId": namespace_id, "scanJobSK": scan_job_sk})
