# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve ``coa_import:dataSourceName`` markers to registered graph sources.

An imported ontology points each graph-backed class at a source by its human name.
Import resolves that name, within the importing namespace, to exactly one registered
``RDF_SPARQL`` source and derives the server-only ``coa:*`` markers from the record —
the author never supplies an internal source id, and a caller-written ``coa:`` marker is
never trusted.

One namespace-scoped read serves every name in the payload. It queries the sources
table's base partition (``PK = NS#{namespace}``), which is strongly consistent: a
source registered moments before the import is always found.

Resolution checks that the source exists, is unambiguous and is a graph source. It does
not probe the graph for data (see the LLD's known limitations).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError
from coa_common.constants import GRAPH_BACKED_SUB_TYPES

# A source on its way out still has a row; binding a class to it would route serve at an
# endpoint that is about to stop being one, and counting it would make a re-registered
# name look ambiguous.
_EXCLUDED_STATUSES = frozenset({"DELETING", "DELETE_FAILED"})


class GraphSourceLookupError(Exception):
    """The sources table could not be read — an infrastructure failure, not bad input."""


class ResolutionFailure(StrEnum):
    """Why a ``dataSourceName`` did not resolve. Values are the finding codes."""

    UNKNOWN = "graph_source_unknown"
    AMBIGUOUS = "graph_source_ambiguous"
    NOT_GRAPH = "graph_source_not_graph"


@dataclass(frozen=True)
class GraphSourceResolution:
    """Outcome for one name: a ``source_id``, or a ``failure`` with its message."""

    source_id: str | None = None
    failure: ResolutionFailure | None = None
    message: str | None = None


def _sources_table() -> Any:
    name = os.getenv("SOURCES_TABLE", "").strip()
    if not name:
        raise GraphSourceLookupError("SOURCES_TABLE is not set; graph source names cannot be resolved")
    return boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")).Table(name)


def _namespace_sources(namespace_id: str) -> list[dict[str, Any]]:
    table = _sources_table()
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"NS#{namespace_id}") & Key("SK").begins_with("SRC#"),
        "ProjectionExpression": "sourceId, #n, sourceSubType, #s",
        "ExpressionAttributeNames": {"#n": "name", "#s": "status"},
        "ConsistentRead": True,
    }
    items: list[dict[str, Any]] = []
    try:
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                return items
            kwargs["ExclusiveStartKey"] = last_key
    except (ClientError, BotoCoreError) as exc:
        raise GraphSourceLookupError(f"Could not read the sources table: {type(exc).__name__}") from exc


def resolve_graph_sources(names: Iterable[str], namespace_id: str) -> dict[str, GraphSourceResolution]:
    """Resolve each source ``name`` to an ``RDF_SPARQL`` source in ``namespace_id``.

    Returns one :class:`GraphSourceResolution` per distinct name. Raises
    :class:`GraphSourceLookupError` only when the table itself cannot be read.
    """
    wanted = set(names)
    if not wanted:
        return {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in _namespace_sources(namespace_id):
        if item.get("status") in _EXCLUDED_STATUSES:
            continue
        name = item.get("name")
        if name in wanted and item.get("sourceId"):
            by_name.setdefault(name, []).append(item)

    results: dict[str, GraphSourceResolution] = {}
    for name in wanted:
        matches = by_name.get(name, [])
        if not matches:
            results[name] = GraphSourceResolution(
                failure=ResolutionFailure.UNKNOWN,
                message=(
                    f'No data source named "{name}" is registered in this namespace; '
                    "register the graph source before importing an ontology that references it"
                ),
            )
        elif len(matches) > 1:
            results[name] = GraphSourceResolution(
                failure=ResolutionFailure.AMBIGUOUS,
                message=f'{len(matches)} data sources are named "{name}" in this namespace; rename one of them',
            )
        elif (sub_type := matches[0].get("sourceSubType", "")) not in GRAPH_BACKED_SUB_TYPES:
            results[name] = GraphSourceResolution(
                failure=ResolutionFailure.NOT_GRAPH,
                message=f'Data source "{name}" is {sub_type or "not typed"}, not a graph (RDF_SPARQL) source',
            )
        else:
            results[name] = GraphSourceResolution(source_id=str(matches[0]["sourceId"]))
    return results
