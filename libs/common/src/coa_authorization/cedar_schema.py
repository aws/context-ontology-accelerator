# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cedar schema constants for the Context Ontology Accelerator (COA)."""

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

# Load the canonical JSON schema
_SCHEMA_PATH = Path(__file__).parent / "cedar-schema.json"
DATA_SCHEMA: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text())

SCL_SCHEMA = "SCL"
"""Cedar namespace prefix used to qualify all entity and action references.

Example usage with the policy evaluator::

    from coa_authorization.cedar_schema import SCL_SCHEMA, EntityType, Action
    from coa_authorization.policy_evaluator import evaluate

    result = evaluate(
        policies=loaded_policies,
        user_id="user-42",
        action="viewNamespace",
        resource_type="Namespace",
        resource_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        global_roles=["VIEWER"],
    )
"""


class EntityType(StrEnum):
    """Cedar entity types representing resources in the SCL authorization model.

    Each member maps to a Cedar entity declared in ``cedar-schema.json``.
    Use these when constructing authorization requests or inspecting results.

    Members:
        USER: An authenticated principal (human or service account).
        NAMESPACE: A logical grouping that scopes all other resources.
        SOURCE: A registered data source (structured database or unstructured
            document source) managed through the unified ``/sources`` API.
        METRIC: A business metric definition.
        ONTOLOGY: A managed ontology within a namespace.
        ONTOLOGY_PROPOSAL: A proposed ontology change produced by induction,
            pending review/acceptance.
    """

    USER = "SCL::User"
    NAMESPACE = "SCL::Namespace"
    SOURCE = "SCL::Source"
    METRIC = "SCL::Metric"
    ONTOLOGY = "SCL::Ontology"
    ONTOLOGY_PROPOSAL = "SCL::OntologyProposal"


class Action(StrEnum):
    """Cedar actions controlling access to SCL resources.

    Actions are grouped by category:

    Namespace lifecycle:
        VIEW_NAMESPACE, MANAGE_NAMESPACE, DELETE_NAMESPACE

    Discovery:
        LIST — enumerate resources the principal can see.

    Read operations:
        QUERY, READ_METRIC, SEARCH_DOCUMENTS

    Write operations:
        MANAGE_SOURCE, MANAGE_METRIC, MANAGE_ONTOLOGY

    Administrative:
        GRANT_PERMISSIONS — assign roles to other principals.
        MANAGE_ROLES — create/edit role definitions.
    """

    # Namespace operations
    VIEW_NAMESPACE = "SCL::Action::viewNamespace"
    MANAGE_NAMESPACE = "SCL::Action::manageNamespace"
    SET_NAMESPACE_STATUS = "SCL::Action::setNamespaceStatus"
    DELETE_NAMESPACE = "SCL::Action::deleteNamespace"
    # Schema-visibility reads (decomposed from viewNamespace so a reporting-only
    # role can run queries while remaining schema-blind). viewSource gates raw
    # source config / Sources->Tables / compiled-SQL schema; viewOntology gates
    # ontology classes/properties, describe_schema, and graph structure.
    VIEW_SOURCE = "SCL::Action::viewSource"
    VIEW_ONTOLOGY = "SCL::Action::viewOntology"
    # List
    LIST = "SCL::Action::list"
    # Read operations
    QUERY = "SCL::Action::query"
    READ_METRIC = "SCL::Action::readMetric"
    SEARCH_DOCUMENTS = "SCL::Action::searchDocuments"
    # Manage operations
    MANAGE_SOURCE = "SCL::Action::manageSource"
    MANAGE_METRIC = "SCL::Action::manageMetric"
    MANAGE_ONTOLOGY = "SCL::Action::manageOntology"
    # Permissions
    GRANT_PERMISSIONS = "SCL::Action::grantPermissions"
    MANAGE_ROLES = "SCL::Action::manageRoles"
