# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Every REST route ``CoaApiClient`` calls, for the integ route-drift guard.

``tests/unit/test_route_lists.py`` parses ``clients/coa_api_client.py``
and fails when these lists and the client's calls diverge, so a new client
route must be added here.

Placeholders: ``{ns}`` is a namespace id, ``{id}`` any other id, ``{uri}`` an
IRI and ``{name}`` a metric name. Query strings mirror what the client sends
and are ignored by the unit guard.
"""

from __future__ import annotations

# Probed authenticated against the integ namespace with dummy ids.
READ_ONLY_ROUTES: list[tuple[str, str]] = [
    # Health and roles
    ("GET", "/health"),
    ("GET", "/system-health"),
    ("GET", "/roles"),
    # Namespaces
    ("GET", "/namespaces"),
    ("GET", "/namespaces/{ns}"),
    ("GET", "/namespaces/{ns}/roles"),
    ("GET", "/namespaces/{ns}/roles/{id}"),
    # Grants
    ("GET", "/namespaces/{ns}/grants"),
    ("GET", "/principals/me/grants"),
    ("GET", "/grants"),
    # Sources
    ("GET", "/namespaces/{ns}/sources?maxResults=1"),
    ("GET", "/namespaces/{ns}/sources/{id}"),
    ("GET", "/namespaces/{ns}/sources/{id}/tables"),
    ("GET", "/namespaces/{ns}/sources/{id}/tables/{id}"),
    ("GET", "/namespaces/{ns}/sources/{id}/scan"),
    ("GET", "/namespaces/{ns}/sources/{id}/scan/{id}"),
    # Induction
    ("GET", "/namespaces/{ns}/induce/jobs"),
    ("GET", "/namespaces/{ns}/induce/jobs/{id}"),
    ("GET", "/namespaces/{ns}/induce/datasources/induced"),
    # Proposals
    ("GET", "/namespaces/{ns}/proposals"),
    ("GET", "/namespaces/{ns}/proposals/{id}"),
    ("GET", "/namespaces/{ns}/proposals/{id}/infer-constraints/jobs/{id}"),
    ("GET", "/namespaces/{ns}/proposals/{id}/validate/jobs/{id}"),
    # Ontologies
    ("GET", "/namespaces/{ns}/ontologies"),
    ("GET", "/namespaces/{ns}/ontologies/{id}"),
    ("GET", "/namespaces/{ns}/ontologies/{id}/download"),
    ("GET", "/namespaces/{ns}/ontologies/{id}/ingest-status/{id}"),
    ("GET", "/namespaces/{ns}/ontology/foundational"),
    # Graph and schema
    ("GET", "/namespaces/{ns}/graph/search?q=probe"),
    ("GET", "/namespaces/{ns}/graph/class?uri={uri}"),
    ("GET", "/namespaces/{ns}/graph/object-property?uri={uri}"),
    ("GET", "/namespaces/{ns}/graph/datatype-property?uri={uri}"),
    ("GET", "/namespaces/{ns}/graph/ontology-overview?ontology_id={uri}"),
    ("GET", "/namespaces/{ns}/schema?includeProperties=false"),
    # Metrics
    ("GET", "/namespaces/{ns}/metrics?maxResults=1"),
    ("GET", "/namespaces/{ns}/metrics/{name}"),
    ("GET", "/namespaces/{ns}/import-jobs/{id}"),
    ("GET", "/namespaces/{ns}/export-osi?format=inline"),
]

# Probed WITHOUT a token and with dummy ids for every placeholder (``{ns}``
# included), so the authorizer refuses the request before any handler runs and
# nothing in a real namespace can be touched.
MUTATING_ROUTES: list[tuple[str, str]] = [
    # Namespaces and grants
    ("POST", "/namespaces"),
    ("PUT", "/namespaces/{ns}"),
    ("PATCH", "/namespaces/{ns}/status"),
    ("DELETE", "/namespaces/{ns}"),
    ("POST", "/namespaces/{ns}/grants"),
    ("DELETE", "/namespaces/{ns}/grants/{id}"),
    ("POST", "/grants"),
    ("DELETE", "/grants/{id}"),
    # Sources
    ("POST", "/namespaces/{ns}/sources"),
    ("POST", "/namespaces/{ns}/sources/upload-urls"),
    ("DELETE", "/namespaces/{ns}/sources/{id}"),
    ("POST", "/namespaces/{ns}/sources/{id}/approve"),
    ("POST", "/namespaces/{ns}/sources/{id}/reject"),
    ("POST", "/namespaces/{ns}/sources/{id}/rescan"),
    ("PUT", "/namespaces/{ns}/sources/{id}/metadata"),
    ("PUT", "/namespaces/{ns}/sources/{id}/rescan-schedule"),
    ("PUT", "/namespaces/{ns}/sources/{id}/event-rescan"),
    ("PUT", "/namespaces/{ns}/sources/{id}/tables/{id}/keep"),
    ("PUT", "/namespaces/{ns}/sources/{id}/tables/{id}/review"),
    ("PUT", "/namespaces/{ns}/sources/{id}/tables/{id}/columns/{name}/review"),
    ("PATCH", "/namespaces/{ns}/sources/{id}/tables/{id}/keys"),
    ("PATCH", "/namespaces/{ns}/sources/{id}/tables/{id}/metadata"),
    ("PATCH", "/namespaces/{ns}/sources/{id}/tables/{id}/columns/{name}/metadata"),
    # Induction and proposals
    ("POST", "/namespaces/{ns}/induce"),
    ("POST", "/namespaces/{ns}/proposals/update"),
    ("POST", "/namespaces/{ns}/proposals/reject"),
    ("POST", "/namespaces/{ns}/proposals/{id}/accept"),
    ("POST", "/namespaces/{ns}/proposals/{id}/cancel"),
    ("POST", "/namespaces/{ns}/proposals/{id}/validate"),
    ("POST", "/namespaces/{ns}/proposals/{id}/infer-constraints"),
    ("POST", "/namespaces/{ns}/proposals/{id}/compile-constraints"),
    ("POST", "/namespaces/{ns}/proposals/{id}/repair-datatypes"),
    ("POST", "/namespaces/{ns}/proposals/{id}/upload-url"),
    # Ontologies
    ("DELETE", "/namespaces/{ns}/ontologies?ontologyId={uri}"),
    ("POST", "/namespaces/{ns}/ontologies/upload-url"),
    ("POST", "/namespaces/{ns}/ontologies/{id}/fetch"),
    ("POST", "/namespaces/{ns}/ontologies/{id}/ingest-from-s3"),
    ("POST", "/namespaces/{ns}/ontology/foundational/{name}/load"),
    # Metrics
    ("POST", "/namespaces/{ns}/metrics"),
    ("POST", "/namespaces/{ns}/metrics/validate"),
    ("PUT", "/namespaces/{ns}/metrics/{name}"),
    ("DELETE", "/namespaces/{ns}/metrics/{name}"),
    ("POST", "/namespaces/{ns}/bulk-delete-metrics"),
    ("POST", "/namespaces/{ns}/import-osi"),
    ("POST", "/namespaces/{ns}/import-osi/upload-url"),
]
