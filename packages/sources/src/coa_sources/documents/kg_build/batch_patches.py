# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""graphrag batch-inference monkeypatches, isolated in a thin leaf module.

These patches run both in the parent (via ``graph_build._setup_graphrag``) and
inside every spawn-started extraction worker (via the rebound pipeline
initializer). Because a spawn worker imports the module that defines the
initializer, this code lives here — a module with only light stdlib/structlog
imports — rather than in ``graph_build``, whose heavy module-level imports
(boto3, llama_index, …) would otherwise be pulled into every worker at startup.

All toolkit / llama_index imports are therefore deferred into the functions.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

logger = structlog.get_logger(__name__)


# Modules that bind ``get_request_body`` and must all be re-pointed at the
# no-temp wrapper. The two batch extractors do
# ``from ...batch_inference_utils import get_request_body`` and then call that
# module-local name in their ``_get_json``, so patching only the utils module
# leaves the extractors — the actual batch-serialization consumers — on the
# original. Spelled out as import paths (not module objects) so the worker-init
# path can re-resolve them in a freshly spawned interpreter.
_GET_REQUEST_BODY_MODULES = (
    "graphrag_toolkit.lexical_graph.indexing.utils.batch_inference_utils",
    "graphrag_toolkit.lexical_graph.indexing.extract.batch_topic_extractor_sync",
    "graphrag_toolkit.lexical_graph.indexing.extract.batch_llm_proposition_extractor_sync",
)


def _make_no_temp_serializer(original, no_temp_models):
    """Wrap get_request_body so a no-temp model's request omits ``temperature``."""

    def _no_temp_get_request_body(llm, messages, inference_parameters):
        is_no_temp = any(model in llm.model for model in no_temp_models)
        if not is_no_temp or "temperature" in inference_parameters:
            return original(llm, messages, inference_parameters)
        # Clear the unconditional read, then remove the field from every shape the
        # serializer emits it in (anthropic.claude: top level; nova:
        # inferenceConfig; meta.llama: parameters).
        body = original(llm, messages, {**inference_parameters, "temperature": 0.0})
        body.pop("temperature", None)
        for container_key in ("inferenceConfig", "parameters"):
            container = body.get(container_key)
            if isinstance(container, dict):
                container.pop("temperature", None)
        return body

    _no_temp_get_request_body._coa_no_temp_wrapped = True  # type: ignore[attr-defined]
    return _no_temp_get_request_body


def _patch_graphrag_batch_request_body() -> None:
    """Keep batch serialization from failing on models that forbid ``temperature``.

    graphrag's ``batch_inference_utils.get_request_body`` reads
    ``inference_parameters['temperature']`` unconditionally for every model
    family. But llama_index's ``BedrockConverse._get_all_kwargs`` DROPS
    ``temperature`` for the models in ``BEDROCK_NO_TEMP_MODELS`` (Opus 4.7/4.8,
    Fable 5, Sonnet 5), because Bedrock rejects the field outright for them. The
    default extraction model (``claude-sonnet-5``) is one of these, so once an
    extraction worker reaches the Bedrock batch minimum (100 records) the
    serializer raises ``KeyError('temperature')`` and the build hits SCAN_FAILED
    before a job is ever submitted.

    Mirror the real-time path, which simply omits the field: wrap
    ``get_request_body`` so that, for a no-temp model, it builds the body with a
    placeholder temperature (to clear the unconditional read) and then strips the
    key back out wherever the family put it. Inserting a real temperature instead
    would re-introduce the field Bedrock refuses, so it must be removed, not
    defaulted.

    Rebinds the name in EVERY module that holds it (``_GET_REQUEST_BODY_MODULES``)
    — the two extractors import it directly, so patching the utils module alone
    misses the real consumers. Idempotent per module via a sentinel attribute, so
    it is safe to re-run (the spawn-worker initializer does exactly that).
    """
    try:
        import importlib

        from llama_index.llms.bedrock_converse.utils import BEDROCK_NO_TEMP_MODELS
    except (ImportError, AttributeError):
        logger.debug("graphrag batch-request-body patch skipped (module not available)")
        return

    for module_path in _GET_REQUEST_BODY_MODULES:
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            logger.debug("batch-request-body patch skipped for missing module", module=module_path)
            continue
        current = getattr(module, "get_request_body", None)
        if current is None or getattr(current, "_coa_no_temp_wrapped", False):
            continue
        # mypy: a dynamically imported ModuleType has no statically-known
        # ``get_request_body`` attribute; rebinding it is the whole point here.
        module.get_request_body = _make_no_temp_serializer(current, BEDROCK_NO_TEMP_MODELS)  # type: ignore[attr-defined]


# Captured in the parent at patch time so ``_batch_worker_init`` can DELEGATE to
# the toolkit's own initializer instead of re-implementing it (and silently
# drifting if the toolkit's ``_init_worker`` grows logic beyond the config
# snapshot). None until the parent patches; a spawned worker re-imports this
# module fresh and re-resolves the original itself (see ``_batch_worker_init``).
_toolkit_init_worker: Callable[..., None] | None = None


def _batch_worker_init(config_snapshot) -> None:
    """Spawn-worker initializer: run the toolkit's init AND the serializer patch.

    ``pipeline_utils.run_pipeline`` runs the extraction transforms in a
    ProcessPoolExecutor started with ``mp_context="spawn"``, whose initializer
    (``_init_worker``) re-applies the GraphRAGConfig snapshot. spawn re-imports
    the toolkit fresh in every worker, so the parent's ``get_request_body`` patch
    is absent there and the no-temp KeyError resurfaces per worker. Replace the
    toolkit initializer with this one (see ``_patch_graphrag_worker_init``) so each
    worker runs the toolkit's own init AND re-applies the serializer patch.

    Delegates to the ORIGINAL ``_init_worker`` rather than reimplementing it, so a
    future toolkit version that does more than apply the snapshot is not silently
    lost in workers. In the parent the original is captured in
    ``_toolkit_init_worker``; in a spawned worker (fresh import, global unset, or
    pointing back at us) it is re-resolved from the freshly-imported, unpatched
    ``pipeline_utils``.

    MUST be a module-level function: spawn pickles the initializer by reference,
    so a closure would not survive the hop.
    """
    init = _toolkit_init_worker
    if init is None or init is _batch_worker_init:
        # Spawned worker (or not yet captured): resolve the original from the
        # freshly-imported, unpatched pipeline_utils and call it directly.
        from graphrag_toolkit.lexical_graph.indexing.utils.pipeline_utils import _init_worker

        _init_worker(config_snapshot)
    else:
        init(config_snapshot)
    _patch_graphrag_batch_request_body()


_graphrag_worker_init_patched = False


def _patch_graphrag_worker_init() -> None:
    """Route spawn extraction workers through ``_batch_worker_init``.

    ``run_pipeline`` reads the module global ``_init_worker`` at call time, so
    rebinding it makes run_pipeline hand our initializer to the
    ProcessPoolExecutor — which is what carries the serializer patch into each
    spawned worker (see ``_batch_worker_init``). The original is captured first so
    the worker initializer can delegate to it.
    """
    global _graphrag_worker_init_patched, _toolkit_init_worker
    if _graphrag_worker_init_patched:
        return
    try:
        from graphrag_toolkit.lexical_graph.indexing.utils import pipeline_utils as _pu
    except (ImportError, AttributeError):
        logger.debug("graphrag worker-init patch skipped (module not available)")
        return
    _toolkit_init_worker = _pu._init_worker
    _pu._init_worker = _batch_worker_init
    _graphrag_worker_init_patched = True
