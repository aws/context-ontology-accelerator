# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Service configuration loaded from environment and SSM."""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import boto3
import structlog
from botocore.exceptions import ClientError
from coa_common import resolve_region, sync_boto_config

logger = structlog.get_logger(__name__)


def _valid_lexical_retriever_strategies() -> set[str]:
    """Return the valid ``RetrieverStrategy`` values for config validation.

    Reuses the ``RetrieverStrategy`` enum from
    ``coa_serve.lexical.strategies`` as the single source of truth.

    The enum is loaded directly from the ``strategies.py`` file (by path) rather
    than via a normal ``import``. Importing
    ``coa_serve.lexical.strategies`` the usual way executes the
    ``lexical`` package ``__init__``, which eagerly imports ``baseline_retriever``
    and thereby pulls in ``graphrag_toolkit`` at config-load time. Loading the
    module file standalone keeps ``strategies.py``'s own lazy-import discipline
    intact, so ``load_config`` never triggers the graphrag/botocore import chain.

    On a broken install (missing ``strategies.py``, unrecognized loader, or an
    exception while executing the module), falls back to a minimal set
    containing only the documented default — ``load_config``'s existing
    validate-or-warn handling will then warn-and-fall-back any non-default
    deployment value, which is the correct behavior when the registry can't be
    consulted.
    """
    fallback: set[str] = {"chunk_based_semantic"}

    strategies_path = Path(__file__).parent / "lexical" / "strategies.py"
    if not strategies_path.exists():
        logger.warning("lexical_strategies_module_missing", path=str(strategies_path))
        return fallback

    spec = importlib.util.spec_from_file_location("_lexical_strategies_for_config", strategies_path)
    if spec is None or spec.loader is None:
        logger.warning("lexical_strategies_spec_unavailable", path=str(strategies_path))
        return fallback

    module = importlib.util.module_from_spec(spec)
    # Register before exec: the strategies module uses `from __future__ import
    # annotations`, so dataclass field-type resolution looks the module up in
    # sys.modules by name while executing. Without this, exec_module raises
    # AttributeError on the frozen StrategySpec dataclass.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return {strategy.value for strategy in module.RetrieverStrategy}
    except Exception as e:
        logger.warning("lexical_strategies_load_failed", error=str(e), error_type=type(e).__name__)
        return fallback
    finally:
        sys.modules.pop(spec.name, None)


@dataclass(frozen=True)
class ServiceConfig:
    """Immutable runtime configuration for the serve component (endpoints, tables, model ids)."""

    guardrail_id: str
    vkg_endpoint: str
    neptune_endpoint: str
    opensearch_endpoint: str
    bedrock_model_id: str
    bedrock_region: str
    data_sources_table: str
    metric_definitions_table: str
    memory_id: str
    session_metadata_table: str
    retrieval_guardrail_id: str = ""
    retrieval_guardrail_version: str = "DRAFT"
    # Tier-1 curated metric SQL budget. Explicitly threaded to the executor so
    # this request path never inherits the protocol's 10-second method default.
    tier1_metric_timeout_s: int = 35
    # Standard-mode Tier-3 engine. "lexical-baseline" + topic_beam is the default
    # because topic_beam is the strongest single-shot strategy on the SEC-10-Q
    # benchmark (45.13% strict vs 34.36% for hand-rolled, 195 questions).
    tier3_strategy: Literal["hand-rolled", "lexical-baseline", "deep-reasoning"] = "lexical-baseline"
    lexical_retriever_strategy: str = "topic_beam"
    # Deep-reasoning Tier-3 budgets (used only when tier3_strategy == "deep-reasoning"
    # or a per-request options.mode=deep-reasoning). Range-validated at load; see
    # load_config.
    deep_reasoning_time_budget_s: int = 30
    deep_reasoning_max_steps: int = 10
    deep_reasoning_per_tool_timeout_s: int = 30
    deep_reasoning_max_fanout: int = 5
    # Wall-clock seconds reserved at session end for the final synthesis call, so
    # a late tool invocation cannot push synthesis past the time budget.
    deep_reasoning_synthesis_reserve_s: int = 8
    # Consecutive no-progress steps a sub-question loop tolerates before stopping
    # with no_new_information. Higher = more escalation attempts (switch tool,
    # reframe, switch modality) before giving up. 1 restores stop-on-first.
    deep_reasoning_max_no_progress_steps: int = 3
    # Where the deep-reasoning Ontology_Lookup_Tool reads the per-namespace ontology.
    # "graph" (default) queries the live published RDF graph in Neptune via the
    # serve graph client — correct for document-induced namespaces, which have no
    # standalone .ttl file. "file" reads a serialized .ttl at
    # deep_reasoning_ontology_file (dev/test convenience); it requires a non-empty,
    # readable path.
    deep_reasoning_ontology_source: Literal["graph", "file"] = "graph"
    deep_reasoning_ontology_file: str = ""
    # How the ontology constrains edge-typed graph traversal. "soft_prior"
    # (default) treats the ontology's edge types as a PREFERENCE — ontology edges
    # are ordered first, but edge types the planner proposes beyond the ontology
    # are still traversed (the induced ontology is a heavily-pruned subset of the
    # graph's predicate vocabulary, so a hard allowlist starves traversal and drops
    # the majority of real edges). "strict" restores the allowlist (ontology edges
    # only), guarded so it never strips the request down to nothing.
    deep_reasoning_ontology_edge_mode: Literal["soft_prior", "strict"] = "soft_prior"


class ConfigurationError(Exception):
    """Raised when required configuration cannot be loaded."""


def env_with_legacy_name(name: str, default: str = "") -> str:
    """Read env var ``name``, honoring its pre-rename ``AGENTIC`` spelling.

    Both "agentic" features were rebranded to "deep reasoning", renaming two env
    families by substring:

    * Tier-3 execution mode — ``AGENTIC_*`` → ``DEEP_REASONING_*``
    * Tier-2 NL→SQL agent   — ``SERVE_AGENTIC_*`` → ``SERVE_DEEP_REASONING_*``

    Substituting ``DEEP_REASONING`` → ``AGENTIC`` covers both, since the ``SERVE_``
    prefix is untouched. A deployment that still sets an old name keeps working (with
    a warning) rather than silently reverting to the default — a silent revert would
    quietly change budgets and timeouts a tuned deployment depends on. Shared with
    :mod:`coa_serve.agents.sql_agent` so the deprecation policy lives in one place.
    Remove this fallback once no deployment sets the old names.
    """
    raw = os.environ.get(name, "")
    if raw:
        return raw
    legacy = name.replace("DEEP_REASONING", "AGENTIC", 1)
    if legacy != name:
        raw = os.environ.get(legacy, "")
        if raw:
            logger.warning("deprecated_env_var", deprecated=legacy, use=name)
            return raw
    return default


def _parse_int_in_range(name: str, default: int, low: int, high: int) -> int:
    """Parse an int env var, falling back to ``default`` when absent/invalid/out-of-range.

    A malformed or out-of-range value logs a warning and returns ``default`` rather
    than raising, so a bad env var can never crash service startup.
    """
    raw = env_with_legacy_name(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid_int_config", name=name, value=raw, fallback=default)
        return default
    if not (low <= value <= high):
        logger.warning("int_config_out_of_range", name=name, value=value, low=low, high=high, fallback=default)
        return default
    return value


_GUARDRAILS_DISABLED_ENV = "SERVE_GUARDRAILS_DISABLED"


def _guardrails_disabled() -> bool:
    """Whether this deployment has opted out of both serve-side Bedrock guardrails.

    Exists for benchmarking, and the reason is measurable rather than a matter of
    taste: the primary guardrail anonymizes PII on INPUT (NAME/EMAIL/PHONE →
    ``{NAME}``), and in a text-to-SQL question the literals ARE the query. "What is
    the border color of card 'Ancestor's Chosen'?" reaches the model as ``'{NAME}'``,
    so the generated ``WHERE`` clause matches no row. Measured over the Tier-2
    benchmark campaign: 1.6-3.2% of BIRD questions per run end up with a placeholder
    in their FINAL SQL, and 306 of those 313 questions (97.8%) scored wrong against a
    47-55% base wrong rate — worth roughly 1pp of execution accuracy per run
    (0.7-1.6pp across five runs), charged to the system under test but caused by the
    guardrail. Blocks are a smaller, separate effect: 944 of 18,305 guardrailed
    Converse calls came back BLOCK, and questions that saw one still scored 52.1% EX
    against 55.5% for those that did not.

    Deployment-scoped on purpose, and deliberately NOT a request option: the primary
    guardrail is the prompt-attack boundary, and a per-request bypass would let any
    caller drop it. Turning it off is a decision for whoever owns the account, taken
    at deploy time (``-c serve_guardrails_disabled=true``) where it is reviewable.

    Setting the SSM parameter ``<prefix>/bedrock/guardrail-id`` to ``none`` has a
    similar effect, but it is not the same thing: that parameter is also read by the
    ingestion and ontology tasks, and ``cdk deploy`` of the guardrail stack writes
    the real id back over it. This flag is serve-only and survives a deploy.
    """
    return os.environ.get(_GUARDRAILS_DISABLED_ENV, "").strip().lower() in ("1", "true", "on", "yes")


def load_config() -> ServiceConfig:
    """Load config from env vars and SSM. Called once at startup."""
    environment = os.environ.get("ENVIRONMENT", "local")
    ssm_prefix = os.environ.get("SSM_PREFIX", "/coa")
    guardrail_id = _get_ssm_parameter(f"{ssm_prefix}/bedrock/guardrail-id", required=False)
    if guardrail_id == "none":
        guardrail_id = ""
    retrieval_guardrail_id = _get_ssm_parameter(f"{ssm_prefix}/bedrock/retrieval-guardrail-id", required=False)
    retrieval_guardrail_version = (
        _get_ssm_parameter(f"{ssm_prefix}/bedrock/retrieval-guardrail-version", required=False) or "DRAFT"
    )
    guardrails_disabled = _guardrails_disabled()
    if guardrails_disabled:
        # ERROR, not warning: a stack serving unguarded traffic must not be
        # something you have to go looking for, and this is the one line that says
        # so. Both ids are cleared rather than one, so there is a single answer to
        # "was the guardrail on?" for any given request.
        logger.error(
            "guardrails_disabled_by_configuration",
            reason=f"{_GUARDRAILS_DISABLED_ENV} is set",
            guardrail_id_ignored=bool(guardrail_id),
            retrieval_guardrail_id_ignored=bool(retrieval_guardrail_id),
            environment=environment,
        )
        guardrail_id = ""
        retrieval_guardrail_id = ""
    tier1_metric_timeout_s = _parse_int_in_range("TIER1_METRIC_TIMEOUT_S", 35, 1, 300)
    logger.info(
        "config_loaded",
        guardrail_configured=bool(guardrail_id),
        retrieval_guardrail_configured=bool(retrieval_guardrail_id),
        guardrails_disabled=guardrails_disabled,
        tier1_metric_timeout_s=tier1_metric_timeout_s,
        environment=environment,
    )

    tier3_strategy = os.environ.get("TIER3_STRATEGY", "lexical-baseline")
    if tier3_strategy == "agentic":
        # Pre-rename spelling of "deep-reasoning". Accepted so an existing deployment
        # that opted into the loop deployment-wide does not silently fall back to the
        # single-shot default on upgrade.
        logger.warning("deprecated_tier3_strategy", deprecated="agentic", use="deep-reasoning")
        tier3_strategy = "deep-reasoning"
    if tier3_strategy not in ("hand-rolled", "lexical-baseline", "deep-reasoning"):
        logger.warning("invalid_tier3_strategy", value=tier3_strategy, fallback="lexical-baseline")
        tier3_strategy = "lexical-baseline"

    # Deep-reasoning Tier-3 budgets: validate-or-fallback within the documented ranges.
    deep_reasoning_time_budget_s = _parse_int_in_range("DEEP_REASONING_TIME_BUDGET_S", 30, 1, 300)
    deep_reasoning_max_steps = _parse_int_in_range("DEEP_REASONING_MAX_STEPS", 10, 1, 50)
    deep_reasoning_per_tool_timeout_s = _parse_int_in_range("DEEP_REASONING_PER_TOOL_TIMEOUT_S", 30, 1, 120)
    deep_reasoning_max_fanout = _parse_int_in_range("DEEP_REASONING_MAX_FANOUT", 5, 1, 20)
    deep_reasoning_synthesis_reserve_s = _parse_int_in_range("DEEP_REASONING_SYNTHESIS_RESERVE_S", 8, 0, 120)
    deep_reasoning_max_no_progress_steps = _parse_int_in_range("DEEP_REASONING_MAX_NO_PROGRESS_STEPS", 3, 1, 20)
    deep_reasoning_ontology_source = env_with_legacy_name("DEEP_REASONING_ONTOLOGY_SOURCE", "graph")
    if deep_reasoning_ontology_source not in ("graph", "file"):
        logger.warning(
            "invalid_deep_reasoning_ontology_source",
            value=deep_reasoning_ontology_source,
            fallback="graph",
        )
        deep_reasoning_ontology_source = "graph"
    deep_reasoning_ontology_file = env_with_legacy_name("DEEP_REASONING_ONTOLOGY_FILE", "")
    # A "file" source with no readable path is a misconfiguration (an empty path
    # makes rdflib parse the CWD → IsADirectoryError at invoke time). Fall back to
    # the graph source rather than ship a source that fails every lookup.
    if deep_reasoning_ontology_source == "file" and (
        not deep_reasoning_ontology_file or not Path(deep_reasoning_ontology_file).is_file()
    ):
        logger.warning(
            "deep_reasoning_ontology_file_invalid_falling_back_to_graph",
            path=deep_reasoning_ontology_file or "(unset)",
        )
        deep_reasoning_ontology_source = "graph"
    deep_reasoning_ontology_edge_mode = env_with_legacy_name("DEEP_REASONING_ONTOLOGY_EDGE_MODE", "soft_prior")
    if deep_reasoning_ontology_edge_mode not in ("soft_prior", "strict"):
        logger.warning(
            "invalid_deep_reasoning_ontology_edge_mode",
            value=deep_reasoning_ontology_edge_mode,
            fallback="soft_prior",
        )
        deep_reasoning_ontology_edge_mode = "soft_prior"

    # Default topic_beam: the strongest single-shot strategy on the SEC-10-Q
    # benchmark (45.13% strict vs 34.36% hand-rolled, 195 questions).
    #
    # The FALLBACK is deliberately chunk_based_semantic, not topic_beam: the guard
    # fires when the value is absent from the registry's valid set, and that set
    # collapses to {"chunk_based_semantic"} when the registry itself cannot be
    # loaded (see _valid_lexical_retriever_strategies). Falling back to topic_beam
    # would "recover" to the value just rejected. chunk_based_semantic is the
    # registry's own DEFAULT_STRATEGY and its broken-install sentinel, so it is the
    # one value guaranteed to be resolvable.
    lexical_retriever_strategy = os.environ.get("LEXICAL_RETRIEVER_STRATEGY", "topic_beam")
    if lexical_retriever_strategy not in _valid_lexical_retriever_strategies():
        logger.warning(
            "invalid_lexical_retriever_strategy",
            value=lexical_retriever_strategy,
            fallback="chunk_based_semantic",
        )
        lexical_retriever_strategy = "chunk_based_semantic"

    return ServiceConfig(
        guardrail_id=guardrail_id,
        retrieval_guardrail_id=retrieval_guardrail_id,
        retrieval_guardrail_version=retrieval_guardrail_version,
        vkg_endpoint=os.environ.get("VKG_ENDPOINT", "http://vkg.local:8080"),
        neptune_endpoint=os.environ.get("NEPTUNE_ENDPOINT", ""),
        opensearch_endpoint=os.environ.get("OPENSEARCH_ENDPOINT", ""),
        bedrock_model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-5"),
        bedrock_region=os.environ.get("BEDROCK_REGION", resolve_region()),
        data_sources_table=os.environ.get("DATA_SOURCES_TABLE", ""),
        metric_definitions_table=os.environ.get("METRIC_DEFINITIONS_TABLE", ""),
        memory_id=os.environ.get("MEMORY_ID", ""),
        session_metadata_table=os.environ.get("SESSION_METADATA_TABLE", ""),
        tier1_metric_timeout_s=tier1_metric_timeout_s,
        tier3_strategy=tier3_strategy,
        lexical_retriever_strategy=lexical_retriever_strategy,
        deep_reasoning_time_budget_s=deep_reasoning_time_budget_s,
        deep_reasoning_max_steps=deep_reasoning_max_steps,
        deep_reasoning_per_tool_timeout_s=deep_reasoning_per_tool_timeout_s,
        deep_reasoning_max_fanout=deep_reasoning_max_fanout,
        deep_reasoning_synthesis_reserve_s=deep_reasoning_synthesis_reserve_s,
        deep_reasoning_max_no_progress_steps=deep_reasoning_max_no_progress_steps,
        deep_reasoning_ontology_source=deep_reasoning_ontology_source,
        deep_reasoning_ontology_file=deep_reasoning_ontology_file,
        deep_reasoning_ontology_edge_mode=deep_reasoning_ontology_edge_mode,
    )


def _get_ssm_parameter(name: str, *, required: bool = False) -> str:
    """Fetch a parameter from SSM Parameter Store.

    Args:
        name: The SSM parameter path.
        required: If True, raises ConfigurationError on failure instead of
                  returning a placeholder. Set to True in non-local environments.
    """
    try:
        region = resolve_region()
        ssm = boto3.client("ssm", region_name=region, config=sync_boto_config())
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "ParameterNotFound":
            if required:
                raise ConfigurationError(f"Required SSM parameter not found: {name}") from e
            logger.warning("ssm_parameter_not_found", name=name)
            return ""
        if required:
            raise ConfigurationError(f"Failed to load SSM parameter {name}: {error_code}") from e
        logger.error("ssm_parameter_error", name=name, error_code=error_code)
        return ""
    except Exception as e:
        if required:
            raise ConfigurationError(f"Failed to load SSM parameter {name}: {e}") from e
        logger.error("ssm_parameter_unexpected_error", name=name, error=str(e))
        return ""
