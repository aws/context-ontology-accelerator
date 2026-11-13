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
# no-temp wrapper: the two batch extractors import it by name, so patching only
# the utils module misses them. Import paths, not module objects, so a spawned
# worker can re-resolve them.
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

    graphrag's ``get_request_body`` reads ``inference_parameters['temperature']``
    unconditionally, but llama_index drops ``temperature`` for the models in
    ``BEDROCK_NO_TEMP_MODELS`` because Bedrock rejects it, so batch serialization
    raises ``KeyError('temperature')`` for them. For a no-temp model, wrap
    ``get_request_body`` to build the body with a placeholder temperature and strip
    the key back out.

    Rebinds the name in every module in ``_GET_REQUEST_BODY_MODULES``. Idempotent
    per module via a sentinel attribute, so it is safe to re-run.
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
        module.get_request_body = _make_no_temp_serializer(current, BEDROCK_NO_TEMP_MODELS)  # type: ignore[attr-defined]


# The toolkit's own initializer, captured in the parent at patch time so
# ``_batch_worker_init`` can delegate to it. None until the parent patches.
_toolkit_init_worker: Callable[..., None] | None = None


def _batch_worker_init(config_snapshot) -> None:
    """Spawn-worker initializer: run the toolkit's init AND the serializer patch.

    ``run_pipeline`` starts its ProcessPoolExecutor with ``mp_context="spawn"``,
    which re-imports the toolkit fresh in every worker, so the parent's
    ``get_request_body`` patch is absent there. This initializer delegates to the
    original ``_init_worker`` (captured in ``_toolkit_init_worker``, or re-resolved
    from ``pipeline_utils`` in a spawned worker) and re-applies the patch.

    MUST be a module-level function: spawn pickles the initializer by reference.
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
