# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Builder for the structured-induction :class:`InductionPipeline`; ``main.py`` wraps it for ``induce_catalog``."""

from coa_common.bedrock import FALLBACK_CHAT_MODEL_ID

from coa_ontology.inducer.services.data_catalog import DataCatalogClient
from coa_ontology.inducer.services.embedding_generator import EmbeddingGeneratorClient
from coa_ontology.inducer.services.ontology_catalog import OntologyCatalogClient
from coa_ontology.inducer.services.pipeline import InductionPipeline


def _build_pipeline(config: dict) -> InductionPipeline:
    return InductionPipeline(
        data_catalog=DataCatalogClient(config["data_catalog_url"]),
        embedding_generator=EmbeddingGeneratorClient(config["embedding_generator_url"]),
        ontology_catalog=OntologyCatalogClient(config["ontology_catalog_url"]),
        llm_region=config.get("llm_region") or "us-east-1",
        grounding_llm_model_id=config.get("grounding_llm_model_id") or FALLBACK_CHAT_MODEL_ID,
    )
