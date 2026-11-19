# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Few-shot SPARQL example loader.

Loads namespace-scoped NL→SPARQL examples published by the Model layer to S3 at
``s3://{ONTOLOGY_BUCKET}/ontologies/{namespace}/{version}/examples.json`` (the
shared contract in ``coa_common.constants.ontology_artifact_s3_key``
/ ``ONTOLOGY_ARTIFACT_EXAMPLES``, schema defined by the loader) and formats them for
injection into the NL→SPARQL prompt, replacing the three hardcoded generic
examples in the system prompt with namespace-specific ones.

Graceful by design (the producer pipeline may not have published a file yet):
- missing file / missing bucket / parse error / bad shape → returns ``[]`` and
  the caller keeps the generic hardcoded examples (the documented fallback).
- results are cached in-memory per (namespace, version); ``invalidate()`` clears
  a namespace's cache on ontology reload.

Expected ``examples.json`` shape (tolerant — extra keys ignored)::

    {"examples": [{"question": "How many open claims?",
                   "sparql": "SELECT (COUNT(?c) AS ?n) WHERE { ?c a ind:Claim ... }"}]}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import structlog
from coa_common import resolve_region

logger = structlog.get_logger(__name__)

_DEFAULT_VERSION = "latest"
_MAX_EXAMPLES = 10  # cap injected examples to bound prompt token cost


@dataclass(frozen=True)
class FewShotExample:
    """A single NL-question / SPARQL pair used as a few-shot prompt example."""

    question: str
    sparql: str


class FewShotExampleLoader:
    """Loads + caches namespace-scoped few-shot examples from S3."""

    def __init__(self, bucket: str | None = None, region: str | None = None, s3_client=None):
        """Configure the S3 source for few-shot examples.

        Args:
            bucket: Ontology-artifacts bucket. Empty/None makes the loader inert.
                Defaults to the ``ONTOLOGY_BUCKET`` env var.
            region: AWS region for the S3 client. Defaults to the resolved region.
            s3_client: Optional injected S3 client (tests pass a stub).
        """
        self._bucket = bucket or os.environ.get("ONTOLOGY_BUCKET", "")
        self._region = region or resolve_region()
        self._s3 = s3_client
        self._cache: dict[tuple[str, str], list[FewShotExample]] = {}

    @property
    def available(self) -> bool:
        """Return whether a bucket is configured (loader is otherwise inert)."""
        return bool(self._bucket)

    def _client(self):
        if self._s3 is None:
            import boto3
            from coa_common import sync_boto_config

            self._s3 = boto3.client("s3", region_name=self._region, config=sync_boto_config())
        return self._s3

    def invalidate(self, namespace: str | None = None) -> None:
        """Clear cache for a namespace (on ontology reload), or all when None."""
        if namespace is None:
            self._cache.clear()
            return
        for key in [k for k in self._cache if k[0] == namespace]:
            del self._cache[key]

    async def load(self, namespace: str, version: str = _DEFAULT_VERSION) -> list[FewShotExample]:
        """Return cached/loaded examples for a namespace; ``[]`` on any failure."""
        if not self.available:
            return []
        cache_key = (namespace, version)
        if cache_key in self._cache:
            return self._cache[cache_key]

        examples = await self._fetch(namespace, version)
        self._cache[cache_key] = examples  # cache even an empty result (avoid re-hitting a missing file)
        return examples

    async def _fetch(self, namespace: str, version: str) -> list[FewShotExample]:
        import asyncio

        from coa_common.constants import ONTOLOGY_ARTIFACT_EXAMPLES, ontology_artifact_s3_key

        try:
            key = ontology_artifact_s3_key(namespace, version, ONTOLOGY_ARTIFACT_EXAMPLES)
        except Exception as exc:
            logger.warning("few_shot_key_invalid", namespace=namespace, error=type(exc).__name__)
            return []

        try:
            client = self._client()
            obj = await asyncio.to_thread(client.get_object, Bucket=self._bucket, Key=key)
            raw = obj["Body"].read()
            data = json.loads(raw)
        except Exception as exc:
            # Missing object (NoSuchKey), access error, or bad JSON — all fall back.
            logger.info("few_shot_load_fallback", namespace=namespace, reason=type(exc).__name__)
            return []

        return self._parse(data, namespace)

    @staticmethod
    def _parse(data, namespace: str) -> list[FewShotExample]:
        raw_examples = data.get("examples") if isinstance(data, dict) else None
        if not isinstance(raw_examples, list):
            logger.warning("few_shot_bad_shape", namespace=namespace)
            return []
        out: list[FewShotExample] = []
        for item in raw_examples:
            if not isinstance(item, dict):
                continue
            question = item.get("question") or item.get("nlQuery") or ""
            sparql = item.get("sparql") or ""
            if question and sparql:
                out.append(FewShotExample(question=str(question), sparql=str(sparql)))
            if len(out) >= _MAX_EXAMPLES:
                break
        logger.info("few_shot_loaded", namespace=namespace, count=len(out))
        return out

    @staticmethod
    def format_for_prompt(examples: list[FewShotExample]) -> str:
        """Render examples as a prompt block; empty string when there are none."""
        if not examples:
            return ""
        blocks = ["Examples (namespace-specific):"]
        for ex in examples:
            blocks.append(f"\nQuestion: {ex.question}\n```sparql\n{ex.sparql}\n```")
        return "\n".join(blocks)
