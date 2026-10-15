# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Neptune Analytics implementation of the LexicalGraphStore protocol.

This backend uses the boto3 ``neptune-graph`` client to execute openCypher
queries and the Property Graph Schema API (``pg_schema``) against a Neptune
Analytics graph.

Configuration is via constructor parameters — no environment variables are
read directly so that the implementation remains testable and composable.

Usage::

    from coa_ontology.inducer.unstructured.stores.na_lexical import (
        NeptuneAnalyticsLexicalStore,
    )

    store = NeptuneAnalyticsLexicalStore(graph_id="g-abc123")
    schema = store.get_graph_schema()
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from coa_ontology.inducer.unstructured.stores.protocol import (
    ClassRecord,
    ClassSchemaResult,
    EntityRecord,
    FactRecord,
    RelationRecord,
    TopicRecord,
)

log = logging.getLogger(__name__)

# Maximum entity IDs per batch for get_facts_for_entities
_BATCH_SIZE = 500

# Default timeout for Neptune Analytics queries (seconds)
_TIMEOUT_SECONDS = 120

# Retry configuration
_MAX_RETRIES = 5


# ── Fact value parsing ─────────────────────────────────────────────────
#
# The graphrag-toolkit lexical graph stores each ``__Fact__`` node with
# a single ``value`` property holding the full fact string formatted
# as ``"<subject_value> <PREDICATE_PHRASE> <object_or_complement>"``.
# There is no separate ``predicate`` or ``complement`` field on the
# node — they have to be parsed out of ``value``. The two helpers
# below implement that parsing.
#
# Why these heuristics work:
#
# 1. The graphrag-toolkit's extraction prompt asks the LLM to format
#    facts as Subject + UPPERCASE_PREDICATE + Object/Complement, and
#    the LLM follows that convention in practice (verified against a
#    populated development graph: 100% of the 20 sampled SPO facts
#    and 20 sampled SPC facts followed this shape).
#
# 2. For SPO facts the object value is also stored on the
#    ``__Entity__`` node connected via ``__OBJECT__``, so we can
#    deterministically strip both the subject prefix AND the object
#    suffix to recover the predicate phrase exactly. The case-only
#    drift the LLM occasionally introduces (``"company"`` vs
#    ``"Company"``) is handled with a case-insensitive match.
#
# 3. For SPC facts no separate object exists, so we fall back to a
#    "longest leading uppercase-words run" heuristic: predicates
#    like ``"TIME PERIOD"`` or ``"INDICATOR OF IMPAIRMENT"`` are
#    consistently uppercase (with a small set of allowed connector
#    tokens — ``OF``, ``IN``, ``ON``, ``TO``, ``BY``, ``FOR``,
#    ``WITH``, ``AS``, ``AT``, ``AND``), and complements like
#    ``"first half of fiscal year 2024"`` are mixed-case starting
#    with a lowercase letter or a number. This is a heuristic and
#    can fail for edge cases (e.g. an all-uppercase complement like
#    ``"USA"``); when it fails, the function returns the entire
#    post-subject string as the predicate and ``None`` for the
#    complement, and Rule 4 will skip the fact.
#
# These parsing rules live here in the store rather than in the
# induction pipeline because the parsing is specific to how the
# graphrag-toolkit encodes facts. A different lexical-graph backend
# might store predicate / complement as separate properties; in
# that case the corresponding store implementation can populate
# ``FactRecord`` directly.

# Stopwords / connectors allowed inside a predicate phrase between
# uppercase content words. These are ALL-CAPS in the lexical graph's
# extraction format. The set is curated from observation of the
# dev graph; expand cautiously (over-permissive entries cause the
# heuristic to swallow part of the complement).
_PREDICATE_CONNECTORS: frozenset[str] = frozenset(
    {
        "OF",
        "IN",
        "ON",
        "TO",
        "BY",
        "FOR",
        "WITH",
        "AS",
        "AT",
        "AND",
        "OR",
        "FROM",
        "INTO",
        "ABOUT",
        "OVER",
        "UNDER",
        "THE",
        "A",
        "AN",
    }
)


def _strip_subject_prefix(fact_value: str, subject_value: str) -> str:
    """Strip the subject prefix from the start of ``fact_value``.

    Performs a case-insensitive prefix match. If the prefix doesn't
    match (e.g. extraction drift), returns ``fact_value`` unchanged.
    Whitespace immediately following the prefix is stripped.

    Examples::

        >>> _strip_subject_prefix("company AUDITS", "company")
        'AUDITS'
        >>> _strip_subject_prefix("Company AUDITS", "company")
        'AUDITS'
        >>> _strip_subject_prefix("AUDITS", "company")
        'AUDITS'
    """
    if fact_value.lower().startswith(subject_value.lower()):
        return fact_value[len(subject_value) :].lstrip()
    return fact_value


def _strip_object_suffix(remainder: str, object_value: str) -> str:
    """Strip the object suffix from the end of ``remainder``.

    Performs a case-insensitive suffix match. If the suffix doesn't
    match, returns ``remainder`` unchanged. Whitespace immediately
    preceding the suffix is stripped.
    """
    if remainder.lower().endswith(object_value.lower()):
        return remainder[: -len(object_value)].rstrip()
    return remainder


def _is_predicate_word(token: str) -> bool:
    """Return True iff ``token`` looks like part of a predicate phrase.

    A token belongs to a predicate phrase if it is either:

    - all-uppercase content (e.g. ``"AUDITS"``, ``"PAYS"``,
      ``"PARTICIPATES_IN"``, ``"INDICATOR"``); OR
    - one of the curated connector words in ``_PREDICATE_CONNECTORS``,
      written in UPPERCASE in the source (e.g. ``"OF"``, ``"FOR"``,
      ``"BY"``). A lowercase ``"of"`` or ``"the"`` belongs to the
      complement, not the predicate — predicates are conventionally
      ALL-CAPS in the lexical graph's extraction format.

    Pure numeric tokens (``"42"``) and tokens with mixed case
    (``"Company"``, ``"first"``) do NOT count — those start the
    complement. Tokens with internal punctuation are stripped of
    surrounding punctuation before the check.
    """
    if not token:
        return False
    # Strip surrounding punctuation but keep internal underscores
    # (predicates like ``RESPONSIBLE_FOR`` are valid).
    stripped = token.strip(",.;:()[]{}\"'")
    if not stripped:
        return False
    # Source must be UPPERCASE (predicates are ALL-CAPS by convention).
    # A lowercase "the" or "of" is part of the complement, not the
    # predicate.
    if not stripped.isupper():
        return False
    if stripped in _PREDICATE_CONNECTORS:
        return True
    # All-uppercase content word — qualify if it has at least one
    # alpha char (so we don't classify ``"123"`` as a predicate word).
    return any(ch.isalpha() for ch in stripped)


# Punctuation stripped from around a caseless predicate token. CJK text uses
# full-width marks, which the ASCII set in ``_is_predicate_word`` does not cover.
_CASELESS_TOKEN_PUNCTUATION = ",.;:()[]{}\"'、。，．；：（）［］｛｝「」『』【】〈〉《》"


def _is_caseless_predicate_word(token: str) -> bool:
    """Return True iff ``token`` can be a predicate in a script without case.

    The extraction prompt asks for predicates in uppercase, and for a
    namespace in another language it tells the model that uppercase applies
    only to letters that have case and that an underscore stands where the
    name would contain a space. A Japanese, Chinese or Korean predicate such
    as ``点検周期`` therefore arrives as one token with no cased letter, and
    ``str.isupper()`` is False for it, so ``_is_predicate_word`` cannot
    accept it.

    A token qualifies when it starts with a letter and contains no cased
    letter at all. Starting with a letter keeps quantities like ``30日``
    out. A token with any cased letter (``KX-7``, ``Company``) is left to
    ``_is_predicate_word``.
    """
    stripped = token.strip(_CASELESS_TOKEN_PUNCTUATION)
    if not stripped or not stripped[0].isalpha():
        return False
    return not any(ch.isupper() or ch.islower() for ch in stripped)


def _extract_spo_predicate(fact_value: str, subject_value: str, object_value: str) -> str:
    """Recover the predicate phrase from an SPO fact's ``value``.

    Strips both the subject prefix and the object suffix
    (case-insensitive) and returns the trimmed remainder. Falls
    back to ``fact_value`` (or the post-subject remainder) when
    the prefix/suffix doesn't match cleanly.

    Examples::

        >>> _extract_spo_predicate(
        ...     "Awardee EMPLOYED BY Company", "Awardee", "company"
        ... )
        'EMPLOYED BY'
        >>> _extract_spo_predicate(
        ...     "Internal Revenue Service AUDITS Company",
        ...     "Internal Revenue Service",
        ...     "company",
        ... )
        'AUDITS'
    """
    after_subj = _strip_subject_prefix(fact_value, subject_value)
    predicate = _strip_object_suffix(after_subj, object_value)
    return predicate.strip()


def _extract_spc_predicate_complement(fact_value: str, subject_value: str) -> tuple[str, str | None]:
    """Recover ``(predicate, complement)`` from an SPC fact's ``value``.

    Strategy:

    1. Strip the subject prefix.
    2. Walk the remaining tokens left-to-right. Tokens that pass
       :func:`_is_predicate_word` belong to the predicate; the first
       token that fails ends the predicate phrase. Everything from
       there to the end of the string is the complement.
    3. In a script without case, the first token is the whole predicate
       when it passes :func:`_is_caseless_predicate_word`, and the rest is
       the complement.

    Returns ``(predicate, None)`` when the heuristic finds zero
    predicate words (e.g. the LLM output didn't follow the
    expected format) — Rule 4 will skip such facts.

    Examples::

        >>> _extract_spc_predicate_complement(
        ...     "Short-term lease expenses TIME PERIOD first half of fiscal year 2024",
        ...     "Short-term lease expenses",
        ... )
        ('TIME PERIOD', 'first half of fiscal year 2024')
        >>> _extract_spc_predicate_complement(
        ...     "company INDICATOR OF IMPAIRMENT decline in stock price",
        ...     "company",
        ... )
        ('INDICATOR OF IMPAIRMENT', 'decline in stock price')
        >>> _extract_spc_predicate_complement(
        ...     "downstream users RESTRICTED ON resale",
        ...     "downstream users",
        ... )
        ('RESTRICTED ON', 'resale')
    """
    after_subj = _strip_subject_prefix(fact_value, subject_value)
    tokens = after_subj.split()
    if not tokens:
        return "", None

    # Walk through tokens building the predicate phrase. A leading
    # connector (``OF``, ``IN`` etc.) without a content uppercase
    # token before it is unlikely — predicates always start with a
    # content word — but the heuristic doesn't enforce that.
    predicate_tokens: list[str] = []
    for i, tok in enumerate(tokens):
        if not predicate_tokens and _is_caseless_predicate_word(tok):
            # A caseless script has no uppercase to mark where the predicate
            # ends, and its complement is often caseless too (``担当者 田中``).
            # The prompt joins a multi-word name with underscores, so the
            # predicate is this one token and the rest is the complement.
            complement = " ".join(tokens[i + 1 :]).strip()
            return tok.strip(_CASELESS_TOKEN_PUNCTUATION), complement or None
        if _is_predicate_word(tok):
            predicate_tokens.append(tok)
        else:
            # First non-predicate word marks the start of the
            # complement.
            complement = " ".join(tokens[i:]).strip()
            predicate = " ".join(predicate_tokens).strip()
            return predicate, complement or None

    # All tokens looked like predicate words — no complement.
    predicate = " ".join(predicate_tokens).strip()
    return predicate, None


class NeptuneAnalyticsLexicalStore:
    """Neptune Analytics backend for the LexicalGraphStore protocol.

    Uses the boto3 ``neptune-graph`` service client to execute openCypher
    queries and the Property Graph Schema API against a Neptune Analytics
    graph endpoint.

    Args:
        graph_id: The Neptune Analytics graph identifier (e.g. "g-abc123").
        region: AWS region where the graph lives (default: "us-east-1").
        endpoint_url: Optional endpoint URL override (for local testing).
        boto_client: Optional pre-configured boto3 client (for testing/DI).
    """

    def __init__(
        self,
        graph_id: str,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
        boto_client: Any | None = None,
    ) -> None:
        """Set up the Neptune Analytics client for the given graph.

        Args:
            graph_id: The Neptune Analytics graph identifier (e.g. "g-abc123").
            region: AWS region where the graph lives.
            endpoint_url: Optional endpoint URL override (for local testing).
            boto_client: Optional pre-configured boto3 client (for testing/DI).

        Raises:
            ValueError: If ``graph_id`` is empty.
        """
        if not graph_id:
            raise ValueError("graph_id is required for NeptuneAnalyticsLexicalStore")

        self._graph_id = graph_id
        self._region = region

        if boto_client is not None:
            self._client = boto_client
        else:
            kwargs: dict[str, Any] = {
                "service_name": "neptune-graph",
                "region_name": region,
            }
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            # Disable SDK retries — Neptune Analytics rejects retried requests
            # with UnprocessableException. We handle retries ourselves.
            boto_cfg = BotoConfig(
                retries={"total_max_attempts": 1, "mode": "standard"},
                read_timeout=_TIMEOUT_SECONDS,
            )
            kwargs["config"] = boto_cfg
            self._client = boto3.client(**kwargs)

    # ── Internal helpers ────────────────────────────────────────────────

    def _execute_query(
        self,
        query: str,
        language: str = "OPEN_CYPHER",
        params: dict | None = None,
    ) -> dict:
        """Execute a query against Neptune Analytics with retry logic.

        Args:
            query: The query string (openCypher or algorithm name).
            language: Query language ("OPEN_CYPHER" or algorithm identifier).
            params: Optional query parameters.

        Returns:
            Parsed JSON response payload.

        Raises:
            RuntimeError: If the query fails after all retries or times out.
        """
        kwargs: dict[str, Any] = {
            "graphIdentifier": self._graph_id,
            "queryString": " ".join(query.split()),
            "language": language,
        }
        if params:
            kwargs["parameters"] = params

        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.execute_query(**kwargs)
                payload = json.loads(response["payload"].read())
                log.debug(
                    "Query response (graph=%s, language=%s):\n  query: %s\n  params: %s\n  payload: %s",
                    self._graph_id,
                    language,
                    " ".join(query.split()),
                    params,
                    json.dumps(payload, default=str)[:4096],
                )
                return payload
            except ClientError as e:
                code = e.response["Error"].get("Code", "")
                message = e.response["Error"].get("Message", str(e))

                # Retryable errors
                if code in ("UnprocessableException", "ConflictException") and attempt < _MAX_RETRIES - 1:
                    wait = 0.1 * (2**attempt)
                    log.warning(
                        "Neptune Analytics %s (attempt %d/%d), retrying in %.1fs: %s",
                        code,
                        attempt + 1,
                        _MAX_RETRIES,
                        wait,
                        message,
                    )
                    time.sleep(wait)
                    continue

                raise RuntimeError(
                    f"Neptune Analytics query failed (backend=neptune_analytics, "
                    f"graph={self._graph_id}, error_code={code}): {message}"
                ) from e
            except Exception as e:
                raise RuntimeError(
                    f"Neptune Analytics query failed (backend=neptune_analytics, graph={self._graph_id}): {e}"
                ) from e

        raise RuntimeError(
            f"Neptune Analytics query failed after {_MAX_RETRIES} attempts "
            f"(backend=neptune_analytics, graph={self._graph_id})"
        )

    def _oc_query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Execute an openCypher query and return the results list.

        Args:
            cypher: openCypher query string.
            params: Optional bound parameters.

        Returns:
            List of result row dicts.
        """
        payload = self._execute_query(cypher, language="OPEN_CYPHER", params=params)
        return payload.get("results", [])

    # ── Protocol implementation ─────────────────────────────────────────

    def get_graph_schema(self) -> ClassSchemaResult:
        """Retrieve class distribution and inter-class relationships.

        Uses the Property Graph Schema API (pg_schema) to first validate
        that the graph contains the expected lexical graph structure
        (__SYS_Class__ nodes and __SYS_RELATION__ edges), then queries
        the pre-computed aggregate nodes for class distribution and
        inter-class relationship data.

        The pg_schema call is efficient (no full graph traversal) and
        confirms the graph is a valid graphrag-toolkit lexical graph
        before issuing the data queries.

        Returns:
            ClassSchemaResult with class records and relation records.

        Raises:
            RuntimeError: If the graph is unreachable or returns an error.
            ValueError: If the graph lacks __SYS_Class__ nodes or the
                schema contains zero class entries.
        """
        # Step 1: Use pg_schema to validate graph structure
        payload = self._execute_query(
            "CALL neptune.graph.pg_schema() YIELD schema RETURN schema",
            language="OPEN_CYPHER",
        )
        results = payload.get("results", [])
        if not results:
            raise ValueError(
                "Property Graph Schema API returned empty results "
                f"(backend=neptune_analytics, graph={self._graph_id}). "
                "The graph may be empty."
            )

        schema = results[0].get("schema", {})
        node_labels = schema.get("nodeLabels", [])
        edge_labels = schema.get("edgeLabels", [])

        if "__SYS_Class__" not in node_labels:
            raise ValueError(
                "Graph does not contain __SYS_Class__ nodes "
                f"(backend=neptune_analytics, graph={self._graph_id}). "
                "The lexical graph may not have been indexed by "
                "graphrag-toolkit. Found node labels: "
                f"{node_labels}"
            )

        if "__SYS_RELATION__" not in edge_labels:
            log.warning(
                "Graph %s has __SYS_Class__ nodes but no __SYS_RELATION__ "
                "edges. Inter-class relationships will be empty.",
                self._graph_id,
            )

        # Step 2: Query the pre-computed aggregate nodes for actual data
        classes = self.get_class_nodes()
        relations = self.get_class_relations()

        if not classes:
            raise ValueError(
                "Graph schema contains zero class entries "
                f"(backend=neptune_analytics, graph={self._graph_id}). "
                "The lexical graph may not have been indexed or contains no "
                "__SYS_Class__ nodes."
            )

        return ClassSchemaResult(classes=classes, relations=relations)

    def get_class_nodes(self) -> list[ClassRecord]:
        """Retrieve all __SYS_Class__ nodes with their labels and counts.

        Returns:
            List of ClassRecord instances, one per distinct classification.
        """
        results = self._oc_query(
            "MATCH (c:`__SYS_Class__`) RETURN c.value AS value, c.count AS count ORDER BY c.count DESC"
        )

        return [
            ClassRecord(
                value=row["value"],
                count=int(row["count"]) if row.get("count") is not None else 0,
            )
            for row in results
            if row.get("value") is not None
        ]

    def get_class_relations(self) -> list[RelationRecord]:
        """Retrieve all __SYS_RELATION__ edges between __SYS_Class__ nodes.

        Returns:
            List of RelationRecord instances representing inter-class
            relationships with their predicate labels and frequencies.
        """
        results = self._oc_query(
            "MATCH (s:`__SYS_Class__`)-[r:`__SYS_RELATION__`]->(t:`__SYS_Class__`) "
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
            if row.get("source_class") is not None
            and row.get("predicate") is not None
            and row.get("target_class") is not None
        ]

    def get_entities_by_class(
        self,
        classification: str,
        limit: int = 100,
    ) -> list[EntityRecord]:
        """Retrieve __Entity__ nodes for a given classification.

        Args:
            classification: The classification string to filter by
                (case-sensitive match on the entity's ``class`` property).
                Note: entity class values use space-separated names
                (e.g. "Financial Instrument") while __SYS_Class__ nodes
                use CamelCase (e.g. "FinancialInstrument").
            limit: Maximum number of entities to return.

        Returns:
            List of EntityRecord instances. Returns an empty list if no
            entities match the classification.
        """
        results = self._oc_query(
            "MATCH (e:`__Entity__`) "
            "WHERE e.class = $classification "
            "RETURN id(e) AS entity_id, e.value AS value, e.class AS classification "
            "LIMIT $limit",
            params={"classification": classification, "limit": limit},
        )

        return [
            EntityRecord(
                entity_id=row["entity_id"],
                value=row["value"],
                classification=row["classification"],
            )
            for row in results
            if row.get("entity_id") is not None and row.get("value") is not None
        ]

    def get_facts_for_entities(
        self,
        entity_ids: list[str],
    ) -> list[FactRecord]:
        """Retrieve __Fact__ nodes for a set of entity IDs.

        Returns both SPO facts (object entity populated) and SPC facts
        (complement populated). Facts with neither are excluded.

        When the entity ID set exceeds 500 IDs, the query is batched
        into chunks of at most 500 IDs per request.

        Args:
            entity_ids: List of entity identifiers to retrieve facts for.

        Returns:
            List of FactRecord instances.
        """
        if not entity_ids:
            return []

        all_facts: list[FactRecord] = []

        # Batch into chunks of _BATCH_SIZE
        for i in range(0, len(entity_ids), _BATCH_SIZE):
            chunk = entity_ids[i : i + _BATCH_SIZE]
            chunk_facts = self._get_facts_batch(chunk)
            all_facts.extend(chunk_facts)

        return all_facts

    def _get_facts_batch(self, entity_ids: list[str]) -> list[FactRecord]:
        """Retrieve facts for a single batch of entity IDs.

        The query traverses from __Entity__ nodes (matched by graph node ID)
        through __Fact__ nodes to capture subject, predicate, object.

        Graph model:
          (subject:__Entity__)-[:__SUBJECT__]->(f:__Fact__)<-[:__OBJECT__]-(object:__Entity__)
          ``__Fact__`` nodes have a single ``value`` property holding the
          full fact string formatted as
          ``"<subject_value> <PREDICATE_PHRASE> <object_or_complement>"``.
          There is NO separate ``predicate`` or ``complement`` property
          on the node — they are encoded inline in ``value``.

          For SPO facts (with ``__OBJECT__`` edge), this method recovers
          the predicate by stripping the subject prefix and object
          suffix from ``f.value``, leaving the predicate phrase in the
          middle. For SPC facts (no ``__OBJECT__`` edge), it uses the
          uppercase-words heuristic in
          :func:`_split_spc_predicate_complement` to split ``f.value``
          (after the subject prefix) into a predicate phrase and a
          complement value.

          The parsing is best-effort. Edge cases:

          - If the subject prefix doesn't match (case-insensitive) the
            start of ``f.value``, the full ``f.value`` is used as the
            predicate as a fallback.
          - For SPO facts, if the object suffix doesn't match the end,
            only the subject prefix is stripped. The remainder may
            include the object's surface form embedded in the
            predicate phrase — acceptable degradation.
          - For SPC facts where the heuristic fails to find an
            uppercase-words run, the entire post-subject string is
            used as the predicate and ``complement`` is set to None.
            Such facts will be filtered out by Rule 4 (which requires
            a non-empty complement).

        Args:
            entity_ids: Batch of entity IDs (at most 500).

        Returns:
            List of FactRecord instances for this batch.
        """
        # Query facts where the subject entity's graph ID is in our set.
        # OBJECT edge goes FROM the object entity TO the fact node.
        results = self._oc_query(
            "MATCH (subj:`__Entity__`)-[:`__SUBJECT__`]->(f:`__Fact__`) "
            "WHERE id(subj) IN $entity_ids "
            "OPTIONAL MATCH (obj:`__Entity__`)-[:`__OBJECT__`]->(f) "
            "RETURN subj.value AS subject_value, subj.class AS subject_class, "
            "f.value AS fact_value, "
            "obj.value AS object_value, obj.class AS object_class",
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
                # SPO fact: parse predicate by stripping subject prefix
                # and object suffix. complement stays None.
                predicate = _extract_spo_predicate(
                    fact_value=fact_value,
                    subject_value=subj_value,
                    object_value=row["object_value"],
                )
                complement = None
            else:
                # SPC fact: split predicate / complement using the
                # uppercase-words heuristic.
                predicate, complement = _extract_spc_predicate_complement(
                    fact_value=fact_value,
                    subject_value=subj_value,
                )

            if not predicate:
                # Defensive: parse failed completely. Skip rather than
                # emit a fact with an empty predicate.
                continue

            facts.append(
                FactRecord(
                    subject_value=subj_value,
                    subject_class=row["subject_class"],
                    predicate=predicate,
                    object_value=row.get("object_value") if has_object else None,
                    object_class=row.get("object_class") if has_object else None,
                    complement=complement,
                )
            )

        return facts

    def get_topics(self) -> list[TopicRecord]:
        """Retrieve all __Topic__ nodes with their connected statement counts.

        Topics with zero connected statements are included with
        statement_count=0.

        Graph model: (__Statement__)-[:__BELONGS_TO__]->(__Topic__)

        Returns:
            List of TopicRecord instances.
        """
        results = self._oc_query(
            "MATCH (t:`__Topic__`) "
            "OPTIONAL MATCH (s:`__Statement__`)-[:`__BELONGS_TO__`]->(t) "
            "WITH t, count(s) AS stmt_count "
            "RETURN id(t) AS topic_id, t.value AS value, stmt_count AS statement_count"
        )

        return [
            TopicRecord(
                topic_id=row["topic_id"],
                value=row["value"],
                statement_count=int(row["statement_count"]) if row.get("statement_count") is not None else 0,
            )
            for row in results
            if row.get("topic_id") is not None and row.get("value") is not None
        ]

    def health_check(self) -> dict:
        """Cheap probe returning backend health status.

        Returns:
            Dict with "status" ("healthy"/"unhealthy") and
            "backend" ("neptune_analytics").
        """
        try:
            results = self._oc_query("RETURN 1 AS ok")
            if results:
                return {"status": "healthy", "backend": "neptune_analytics"}
            return {"status": "unhealthy", "backend": "neptune_analytics"}
        except Exception as e:
            log.warning("Health check failed for graph %s: %s", self._graph_id, e)
            return {"status": "unhealthy", "backend": "neptune_analytics"}
