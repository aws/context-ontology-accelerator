# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Neptune Database implementation of the LexicalGraphStore protocol.

Uses the ``neptunedata`` (boto3) openCypher API to query a Neptune DB
cluster hosting a graphrag-toolkit lexical graph. Reuses the same
openCypher queries as the Neptune Analytics implementation but routes
them through the ``neptunedata`` ``execute_open_cypher_query`` API.

The transport is IAM-authenticated via SigV4 (handled by boto3 session).
"""

from __future__ import annotations

import json
import logging
import re
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from coa_ontology.inducer.unstructured.stores.na_lexical import (
    _BATCH_SIZE,
    _MAX_RETRIES,
    _extract_spc_predicate_complement,
    _extract_spo_predicate,
)
from coa_ontology.inducer.unstructured.stores.protocol import (
    ClassRecord,
    ClassSchemaResult,
    EntityRecord,
    FactRecord,
    RelationRecord,
    TopicRecord,
)

log = logging.getLogger(__name__)

# Defense-in-depth: `_label` interpolates ``self._tenant_id`` into a
# backtick-quoted openCypher label. If a non-hex character (backtick, quote,
# semicolon, whitespace, …) ever reaches this constructor, the label
# terminates and arbitrary cypher can be injected against the Neptune
# cluster. The primary check happens at :func:`to_graphrag_tenant_id`; this
# is the sink-side belt to that suspenders.
_TENANT_ID_HEX_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)


class NeptuneDatabaseLexicalStore:
    """LexicalGraphStore backed by a Neptune DB cluster (openCypher via neptunedata API).

    Args:
        endpoint: Neptune DB cluster endpoint.
        port: Neptune port (default 8182).
        region: AWS region for SigV4 signing.
        tenant_id: Tenant ID suffix for multi-tenant label scoping (e.g. "389030abe759468eb64ae200f").
            Must be hex-only or empty; non-hex input raises ``ValueError``.
    """

    def __init__(self, endpoint: str, port: int = 8182, region: str = "us-east-1", tenant_id: str = "") -> None:
        """Set up the neptunedata openCypher client for the given Neptune DB cluster.

        Args:
            endpoint: Neptune DB cluster endpoint.
            port: Neptune port.
            region: AWS region for SigV4 signing.
            tenant_id: Tenant ID suffix for multi-tenant label scoping. Must
                be hex-only (post-hyphen-strip form of a UUID) or empty.

        Raises:
            ValueError: If ``tenant_id`` is non-empty and contains any
                character outside ``[0-9a-fA-F]``.
        """
        if tenant_id and not _TENANT_ID_HEX_RE.match(tenant_id):
            raise ValueError(f"tenant_id must be hex-only or empty; got {tenant_id!r}")
        self._endpoint = endpoint
        self._port = port
        self._region = region
        self._tenant_id = tenant_id
        endpoint_url = f"https://{endpoint}:{port}"
        config = Config(
            retries={"total_max_attempts": 1, "mode": "standard"},
            read_timeout=600,
        )
        session = boto3.Session(region_name=region)
        self._client = session.client("neptunedata", endpoint_url=endpoint_url, config=config)

    def _label(self, base: str) -> str:
        """Scope a label with tenant ID if set: __Entity__ → __Entity__<tenant_id>__."""
        if self._tenant_id:
            return f"`__{base}__{self._tenant_id}__`"
        return f"`__{base}__`"

    def _oc_query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Execute an openCypher query and return the results list."""
        query_str = " ".join(cypher.split())
        kwargs: dict = {"openCypherQuery": query_str}
        if params:
            kwargs["parameters"] = json.dumps(params)

        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.execute_open_cypher_query(**kwargs)
                return response.get("results", [])
            except ClientError as e:
                code = e.response["Error"].get("Code", "")
                message = e.response["Error"].get("Message", str(e))
                if code in ("ConcurrentModificationException",) and attempt < _MAX_RETRIES - 1:
                    time.sleep(0.1 * (2**attempt))
                    continue
                raise RuntimeError(
                    f"Neptune DB query failed (endpoint={self._endpoint}, error_code={code}): {message}"
                ) from e

        raise RuntimeError(f"Neptune DB query failed after {_MAX_RETRIES} attempts")

    # ── Protocol implementation ─────────────────────────────────────────

    def get_class_nodes(self) -> list[ClassRecord]:
        """Return all class nodes with their occurrence counts, most frequent first.

        Returns:
            One :class:`ClassRecord` per ``SYS_Class`` node in the graph.
        """
        results = self._oc_query(
            f"MATCH (c:{self._label('SYS_Class')}) RETURN c.value AS value, c.count AS count ORDER BY c.count DESC"
        )
        return [
            ClassRecord(value=row["value"], count=int(row["count"]) if row.get("count") is not None else 0)
            for row in results
            if row.get("value") is not None
        ]

    def get_class_relations(self) -> list[RelationRecord]:
        """Return the class-to-class relations with their occurrence counts.

        Returns:
            One :class:`RelationRecord` per ``SYS_RELATION`` edge between class nodes.
        """
        results = self._oc_query(
            f"MATCH (s:{self._label('SYS_Class')})-[r:`__SYS_RELATION__`]->(t:{self._label('SYS_Class')}) "
            "RETURN s.value AS source_class, r.value AS predicate, "
            "t.value AS target_class, r.count AS count"
        )
        return [
            RelationRecord(
                source_class=row["source_class"],
                predicate=row["predicate"],
                target_class=row["target_class"],
                count=int(row["count"]) if row.get("count") is not None else 0,
            )
            for row in results
            if row.get("source_class") and row.get("predicate") and row.get("target_class")
        ]

    def get_entities_by_class(self, classification: str, limit: int = 100) -> list[EntityRecord]:
        """Return entities whose classification matches, up to a limit.

        Args:
            classification: Class value to filter entities by.
            limit: Maximum number of entities to return.

        Returns:
            The matching :class:`EntityRecord` list.
        """
        results = self._oc_query(
            f"MATCH (e:{self._label('Entity')}) "
            "WHERE e.class = $classification "
            "RETURN id(e) AS entity_id, e.value AS value, e.class AS classification "
            "LIMIT $limit",
            params={"classification": classification, "limit": limit},
        )
        return [
            EntityRecord(entity_id=row["entity_id"], value=row["value"], classification=row["classification"])
            for row in results
            if row.get("entity_id") is not None and row.get("value") is not None
        ]

    def get_facts_for_entities(self, entity_ids: list[str]) -> list[FactRecord]:
        """Return the facts asserted about the given entities, batching the lookup.

        Args:
            entity_ids: Internal ids of the subject entities to fetch facts for.

        Returns:
            The :class:`FactRecord` list across all batches; empty if no ids given.
        """
        if not entity_ids:
            return []
        all_facts: list[FactRecord] = []
        for i in range(0, len(entity_ids), _BATCH_SIZE):
            chunk = entity_ids[i : i + _BATCH_SIZE]
            all_facts.extend(self._get_facts_batch(chunk))
        return all_facts

    def _get_facts_batch(self, entity_ids: list[str]) -> list[FactRecord]:
        results = self._oc_query(
            f"MATCH (subj:{self._label('Entity')})-[:`__SUBJECT__`]->(f:{self._label('Fact')}) "
            "WHERE id(subj) IN $entity_ids "
            f"OPTIONAL MATCH (obj:{self._label('Entity')})-[:`__OBJECT__`]->(f) "
            "RETURN subj.value AS subject_value, subj.class AS subject_class, "
            "f.value AS fact_value, obj.value AS object_value, obj.class AS object_class",
            params={"entity_ids": entity_ids},
        )
        facts: list[FactRecord] = []
        for row in results:
            fact_value = row.get("fact_value")
            subj_value = row.get("subject_value")
            if not fact_value or not subj_value:
                continue

            has_object = row.get("object_value") is not None

            if has_object:
                predicate = _extract_spo_predicate(
                    fact_value=fact_value,
                    subject_value=subj_value,
                    object_value=row["object_value"],
                )
                complement = None
            else:
                predicate, complement = _extract_spc_predicate_complement(
                    fact_value=fact_value,
                    subject_value=subj_value,
                )

            if not predicate:
                continue

            facts.append(
                FactRecord(
                    subject_value=subj_value,
                    subject_class=row.get("subject_class", ""),
                    predicate=predicate,
                    object_value=row.get("object_value") if has_object else None,
                    object_class=row.get("object_class") if has_object else None,
                    complement=complement,
                )
            )
        return facts

    def get_topics(self) -> list[TopicRecord]:
        """Return all topic nodes with the count of statements belonging to each.

        Returns:
            One :class:`TopicRecord` per ``Topic`` node in the graph.
        """
        results = self._oc_query(
            f"MATCH (t:{self._label('Topic')}) "
            f"OPTIONAL MATCH (s:{self._label('Statement')})-[:`__BELONGS_TO__`]->(t) "
            "WITH t, count(s) AS stmt_count "
            "RETURN id(t) AS topic_id, t.value AS value, stmt_count AS statement_count"
        )
        return [
            TopicRecord(
                topic_id=row["topic_id"],
                value=row["value"],
                statement_count=int(row.get("statement_count", 0)),
            )
            for row in results
            if row.get("topic_id") is not None and row.get("value") is not None
        ]

    def get_graph_schema(self) -> ClassSchemaResult:
        """Return the graph schema: its class nodes and their relations.

        Returns:
            A :class:`ClassSchemaResult` combining class nodes and relations.

        Raises:
            ValueError: If the graph contains no class entries.
        """
        classes = self.get_class_nodes()
        if not classes:
            raise ValueError("Graph contains zero class entries — not a valid lexical graph")
        relations = self.get_class_relations()
        return ClassSchemaResult(classes=classes, relations=relations)

    def health_check(self) -> dict:
        """Probe the Neptune DB connection with a trivial query.

        Returns:
            A status dict reporting healthy/unhealthy plus the backend name and
            any error message.
        """
        try:
            self._oc_query("RETURN 1 AS ping")
            return {"status": "healthy", "backend": "neptune_db"}
        except Exception as e:
            return {"status": "unhealthy", "backend": "neptune_db", "error": str(e)}
