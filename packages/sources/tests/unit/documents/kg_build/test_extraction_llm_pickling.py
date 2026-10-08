# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The extraction LLM's trip into a ProcessPool worker.

Deliberately a separate module from test_graph_build.py. That file stubs
``llama_index.llms.bedrock_converse`` in ``sys.modules`` so it can run without the
kg-build image's dependencies, and a stub cannot exercise pydantic's
``__getstate__``/``__setstate__`` — which is the entire mechanism under test here.
Importing the real package in a process where the stub is already installed silently
yields the stub, so these assertions have to live where nothing has stubbed it.
"""

from __future__ import annotations

import pickle

import pytest

pytestmark = pytest.mark.unit

_bedrock_converse_module = pytest.importorskip(
    "llama_index.llms.bedrock_converse",
    reason="only the kg-build image installs llama_index",
)
if getattr(_bedrock_converse_module, "__coa_test_stub__", False):
    pytest.skip("real llama_index is unavailable", allow_module_level=True)

_MODEL_ARN = "arn:aws:bedrock:us-east-1:1:inference-profile/us.anthropic.claude-sonnet-5"


def _graph_build():
    from coa_sources.documents.kg_build import graph_build

    return graph_build


class TestPicklableExtractionLlm:
    @pytest.mark.parametrize("state", [None, {}, {"__dict__": None}])
    def test_malformed_pickle_state_fails_with_a_clear_error(self, state):
        cls = _graph_build()._picklable_bedrock_converse_cls()
        uninitialized = object.__new__(cls)

        with pytest.raises(TypeError, match="pickle state"):
            uninitialized.__setstate__(state)

    def test_upstream_loses_its_client_on_a_round_trip(self):
        """Establishes the defect the subclass exists to fix.

        If this ever starts passing, upstream has fixed the round-trip and
        _PicklableBedrockConverse is dead weight that should be removed.
        """
        from llama_index.llms.bedrock_converse import BedrockConverse

        llm = BedrockConverse(model=_MODEL_ARN, max_tokens=16384, timeout=300.0, region_name="us-east-1")
        restored = pickle.loads(pickle.dumps(llm))

        assert not hasattr(restored, "_client"), "upstream now survives pickling; drop the subclass"

    def test_client_survives_a_round_trip(self):
        """The assertion that pins the fix.

        Extraction runs in ProcessPool workers, so a client that does not survive
        pickling is a client whose settings never apply — LLMCache quietly rebuilds one
        at 60s with a default pool, which is how a run configured for 300s died on
        ReadTimeoutError after 45 minutes.
        """
        from botocore.config import Config

        cls = _graph_build()._picklable_bedrock_converse_cls()
        llm = cls(
            model=_MODEL_ARN,
            max_tokens=16384,
            timeout=300.0,
            max_retries=5,
            region_name="us-east-1",
            botocore_config=Config(max_pool_connections=16, connect_timeout=10, read_timeout=300.0),
        )
        restored = pickle.loads(pickle.dumps(llm))

        assert hasattr(restored, "_client"), "worker would fall back to the toolkit's 60s client"
        assert restored._client.meta.config.max_pool_connections == 16
        assert restored._client.meta.config.read_timeout == 300.0

    def test_retry_and_timeout_settings_survive_a_round_trip(self):
        """``__setstate__`` keeps only the fields the current ``__init__`` accepts.

        If an upstream signature change dropped ``max_retries`` or ``botocore_config``,
        the worker would rebuild the client from upstream defaults without any error.
        The values here differ from those defaults on purpose: without
        ``botocore_config`` the client would take ``connect_timeout`` from ``timeout``
        and its retries from ``max_retries``, so each assertion fails if its field is
        lost.
        """
        from botocore.config import Config

        cls = _graph_build()._picklable_bedrock_converse_cls()
        llm = cls(
            model=_MODEL_ARN,
            max_tokens=16384,
            timeout=300.0,
            max_retries=5,
            region_name="us-east-1",
            botocore_config=Config(
                connect_timeout=7,
                read_timeout=300.0,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        restored = pickle.loads(pickle.dumps(llm))

        assert restored.max_retries == 5, "the llama_index retry layer would revert to its default"
        assert restored.timeout == 300.0
        config = restored._client.meta.config
        assert config.connect_timeout == 7, "botocore_config was lost; connect timeout came from timeout"
        assert config.read_timeout == 300.0
        assert config.retries["mode"] == "standard"
        assert config.retries.get("total_max_attempts", config.retries.get("max_attempts", 0) + 1) == 4

    def test_the_class_is_resolvable_by_name(self):
        """Pickle stores a class by module + qualname and re-looks it up on load.

        A class built inside a function ("Can't get local object") or attached to the
        module on first call ("Can't get attribute ... on module", under spawn, where
        the worker re-imports from source) both fail there. Both were tried.
        """
        import importlib

        cls = _graph_build()._picklable_bedrock_converse_cls()
        module = importlib.import_module(cls.__module__)

        assert getattr(module, cls.__qualname__, None) is cls

    def test_temperature_is_stripped_for_the_configured_model(self):
        """Tripwire for the upstream allowlist.

        Bedrock rejects ``temperature`` for claude-sonnet-5, and llama_index strips it
        for models in BEDROCK_NO_TEMP_MODELS — the only reason passing
        ``temperature=0.0`` is safe. If that stops covering the configured model, fail
        here rather than with a ValidationException part-way through a run.
        The model id is a literal rather than a constant. The extraction model is
        deployment config (``BEDROCK_MODEL_ARN`` on the task definition), so there is
        no constant that names it; borrowing one that names a different model would
        make this tripwire assert about something the extraction path never uses.
        """
        cls = _graph_build()._picklable_bedrock_converse_cls()
        llm = cls(
            model=_MODEL_ARN,
            temperature=0.0,
            max_tokens=16384,
            region_name="us-east-1",
        )

        assert "temperature" not in llm._model_kwargs
