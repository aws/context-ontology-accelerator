# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deletion pipeline step: delete all ontology data + S3 artifacts.

Reuses the existing ontology-engine HTTP teardown and S3 sweep from
``cleanup.py`` — this step just runs them as their own Step Functions task
so the ontology-engine's slow, best-effort HTTP call (up to 120s) has its
own retry policy separate from the rest of the pipeline.
"""

from __future__ import annotations

from typing import Any

from coa_control_plane.namespace.cleanup import (
    delete_all_ontologies,
    delete_ontology_artifacts,
)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Step Functions task: delete all ontology data for the namespace.

    Input: {"namespaceId": str}
    Output: {"namespaceId": str}

    ``delete_all_ontologies`` raises on purpose: the step retries and then lands
    the namespace in DELETE_FAILED rather than orphaning its AOSS vector index.
    ``delete_ontology_artifacts`` (S3) stays best-effort.
    """
    namespace_id = event["namespaceId"]
    delete_all_ontologies(namespace_id)
    delete_ontology_artifacts(namespace_id)
    return {"namespaceId": namespace_id}
