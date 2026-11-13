# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the class description generator."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.inducer.unstructured.services.description_generator import (
    FALLBACK_TOP_N,
    MAX_PROMPT_LABELS,
    _build_user_prompt,
    _fallback_description,
    _post_process_response,
    generate_descriptions,
)


def _ok_client(text: str = "A concise class description.") -> MagicMock:
    """Create a mock LLM client whose ``generate`` returns ``text``."""
    client = MagicMock()
    client.generate.return_value = text
    return client


def _failing_client(exc: BaseException) -> MagicMock:
    """Create a mock LLM client whose ``generate`` raises ``exc``."""
    client = MagicMock()
    client.generate.side_effect = exc
    return client


class TestSuccessPath:
    """Successful LLM description generation."""

    def test_returns_llm_response_as_description(self):
        """LLM-generated text becomes the description verbatim."""
        client = _ok_client("Financial instruments including bonds and notes.")
        class_entities = {
            "Financial Instrument": ["Treasury Bond", "Corporate Note", "Equity Swap"],
        }

        result = generate_descriptions(class_entities, client)

        assert result == {
            "Financial Instrument": "Financial instruments including bonds and notes.",
        }

    def test_llm_called_with_system_and_user_prompt(self):
        """Both prompt and system arguments must be populated."""
        client = _ok_client("desc")
        class_entities = {"Company": ["Acme Corp", "Globex"]}

        generate_descriptions(class_entities, client)

        assert client.generate.call_count == 1
        call = client.generate.call_args
        # First positional argument is the user prompt.
        user_prompt = call.args[0]
        # ``system`` is passed as a keyword argument.
        system_prompt = call.kwargs["system"]

        assert isinstance(user_prompt, str) and user_prompt.strip()
        assert isinstance(system_prompt, str) and system_prompt.strip()

    def test_user_prompt_includes_class_label_and_entity_labels(self):
        """The LLM input must contain the class and example entities."""
        client = _ok_client("desc")
        class_entities = {
            "Company": ["Acme Corp", "Globex Industries", "Initech"],
        }

        generate_descriptions(class_entities, client)

        user_prompt = client.generate.call_args.args[0]
        assert "Company" in user_prompt
        assert "Acme Corp" in user_prompt
        # At least one of the other labels also makes it into the prompt.
        assert "Globex Industries" in user_prompt or "Initech" in user_prompt

    def test_one_llm_call_per_class(self):
        """Each class with a non-empty label list invokes the LLM exactly once."""
        client = _ok_client("desc")
        class_entities = {
            "Company": ["Acme"],
            "Person": ["Alice"],
            "Country": ["France"],
        }

        result = generate_descriptions(class_entities, client)

        assert client.generate.call_count == 3
        assert set(result.keys()) == {"Company", "Person", "Country"}


class TestFallbackOnLLMFailure:
    """Per-class fallback when the LLM call fails."""

    def test_runtime_error_triggers_fallback(self, caplog):
        """``RuntimeError`` (the documented LLMClient failure) → fallback."""
        client = _failing_client(RuntimeError("Bedrock unavailable"))
        class_entities = {"Company": ["Acme", "Globex", "Initech"]}

        with caplog.at_level(logging.WARNING):
            result = generate_descriptions(class_entities, client)

        assert result["Company"].startswith("Class representing 3 entities including:")
        assert "Company" in caplog.text
        assert "fallback" in caplog.text.lower()

    def test_generic_exception_triggers_fallback(self):
        """Generic ``Exception`` is caught by the broad handler."""
        client = _failing_client(Exception("unexpected"))
        class_entities = {"Company": ["Acme", "Globex"]}

        result = generate_descriptions(class_entities, client)

        assert result["Company"].startswith("Class representing 2 entities including:")

    def test_timeout_error_triggers_fallback(self):
        """A timeout from the LLM client → fallback (no exception bubbles)."""
        client = _failing_client(TimeoutError("LLM call timed out"))
        class_entities = {"Company": ["Acme", "Globex"]}

        result = generate_descriptions(class_entities, client)

        assert result["Company"].startswith("Class representing 2 entities including:")

    def test_empty_response_triggers_fallback(self):
        """An empty LLM response is treated as failure → fallback."""
        client = _ok_client("")
        class_entities = {"Company": ["Acme", "Globex"]}

        result = generate_descriptions(class_entities, client)

        assert result["Company"].startswith("Class representing 2 entities including:")

    def test_whitespace_only_response_triggers_fallback(self):
        """A whitespace-only LLM response → fallback after stripping."""
        client = _ok_client("   \n\t  ")
        class_entities = {"Company": ["Acme", "Globex"]}

        result = generate_descriptions(class_entities, client)

        assert result["Company"].startswith("Class representing 2 entities including:")


class TestFallbackFormat:
    """Deterministic fallback string format."""

    def test_top_5_alphabetical_when_more_than_five_labels(self):
        """Only the lexicographically smallest 5 labels are listed."""
        client = _failing_client(RuntimeError("boom"))
        labels = ["Zeta", "Yankee", "Bravo", "Alpha", "Charlie", "Delta", "Echo"]
        class_entities = {"Letter": labels}

        result = generate_descriptions(class_entities, client)

        # Sorted alphabetically: Alpha, Bravo, Charlie, Delta, Echo, Yankee, Zeta
        # Top 5 = Alpha, Bravo, Charlie, Delta, Echo.
        expected = "Class representing 7 entities including: Alpha, Bravo, Charlie, Delta, Echo"
        assert result["Letter"] == expected

    def test_count_reflects_total_not_top_n(self):
        """``{N}`` is the total label count, not the truncated top-5."""
        client = _failing_client(RuntimeError("boom"))
        labels = [f"label-{i:02d}" for i in range(20)]  # 20 labels
        class_entities = {"Item": labels}

        result = generate_descriptions(class_entities, client)

        # Number BEFORE "entities" must be the total (20), not the 5 listed.
        assert result["Item"].startswith("Class representing 20 entities including:")
        # And only top_n labels are listed.
        listed = result["Item"].split("including: ", 1)[1].split(", ")
        assert len(listed) == FALLBACK_TOP_N

    def test_fewer_than_five_labels_listed_in_alphabetical_order(self):
        """When N < 5, all labels appear, alphabetically sorted."""
        client = _failing_client(RuntimeError("boom"))
        class_entities = {"Tiny": ["gamma", "alpha", "beta"]}

        result = generate_descriptions(class_entities, client)

        assert result["Tiny"] == "Class representing 3 entities including: alpha, beta, gamma"

    def test_case_insensitive_alphabetical_sort(self):
        """Ordering uses ``str.casefold`` so case does not flip order."""
        # A case-sensitive sort would give ["Alpha", "GAMMA", "beta", "delta"].
        client = _failing_client(RuntimeError("boom"))
        class_entities = {"Mixed": ["beta", "Alpha", "GAMMA", "delta"]}

        result = generate_descriptions(class_entities, client)

        assert result["Mixed"] == ("Class representing 4 entities including: Alpha, beta, delta, GAMMA")

    def test_exactly_five_labels_all_listed(self):
        """Boundary: with exactly 5 labels, all of them appear sorted."""
        client = _failing_client(RuntimeError("boom"))
        class_entities = {"Five": ["e", "d", "c", "b", "a"]}

        result = generate_descriptions(class_entities, client)

        assert result["Five"] == "Class representing 5 entities including: a, b, c, d, e"


class TestEmptyLabelList:
    """Empty / zero-label classes."""

    def test_zero_labels_produces_valid_fallback_without_llm_call(self):
        """A class with zero labels uses the fallback and skips the LLM."""
        client = _ok_client("should not be used")
        class_entities = {"Empty": []}

        result = generate_descriptions(class_entities, client)

        assert result == {"Empty": "Class representing 0 entities including: "}
        client.generate.assert_not_called()

    def test_zero_labels_logs_warning(self, caplog):
        """An empty-label class emits a WARNING explaining the fallback."""
        client = _ok_client("should not be used")
        class_entities = {"Empty": []}

        with caplog.at_level(logging.WARNING):
            generate_descriptions(class_entities, client)

        assert "Empty" in caplog.text
        assert "no entity labels" in caplog.text.lower()


class TestFaultIsolation:
    """A failure on one class does not abort the batch."""

    def test_one_failure_one_success_yields_both_entries(self):
        """Mixed failures and successes both end up in the result dict."""
        client = MagicMock()
        # First call (Company) fails; second call (Person) succeeds.
        client.generate.side_effect = [
            RuntimeError("first call fails"),
            "People are individual human beings.",
        ]
        class_entities = {
            "Company": ["Acme Corp", "Globex"],
            "Person": ["Alice", "Bob"],
        }

        result = generate_descriptions(class_entities, client)

        assert set(result.keys()) == {"Company", "Person"}
        assert result["Company"].startswith("Class representing 2 entities including:")
        assert result["Person"] == "People are individual human beings."

    def test_no_exception_bubbles_when_all_classes_fail(self):
        """If every class's LLM call fails, the function still returns cleanly."""
        client = _failing_client(RuntimeError("everything is on fire"))
        class_entities = {
            "Company": ["Acme", "Globex"],
            "Person": ["Alice", "Bob"],
        }

        result = generate_descriptions(class_entities, client)

        assert set(result.keys()) == {"Company", "Person"}
        for desc in result.values():
            assert desc.startswith("Class representing 2 entities including:")


class TestEmptyInputDict:
    """Empty input dict short-circuits to an empty result."""

    def test_empty_dict_returns_empty_dict(self):
        client = _ok_client("desc")
        assert generate_descriptions({}, client) == {}
        client.generate.assert_not_called()


class TestResponsePostProcessing:
    """LLM response cleaning before storage.

    Validates: the implementation strips a single pair of surrounding double
    quotes (a common LLM artifact) and trims surrounding whitespace.
    """

    def test_surrounding_double_quotes_stripped(self):
        """Quoted-echo style responses have the outer quotes removed."""
        client = _ok_client('"Cars are wheeled vehicles used for transport."')
        class_entities = {"Car": ["Sedan", "SUV"]}

        result = generate_descriptions(class_entities, client)

        assert result["Car"] == "Cars are wheeled vehicles used for transport."

    def test_leading_and_trailing_whitespace_stripped(self):
        client = _ok_client("   Cars are wheeled vehicles.\n\n")
        class_entities = {"Car": ["Sedan", "SUV"]}

        result = generate_descriptions(class_entities, client)

        assert result["Car"] == "Cars are wheeled vehicles."

    def test_unquoted_response_unchanged_other_than_whitespace(self):
        client = _ok_client("Cars are wheeled vehicles.")
        class_entities = {"Car": ["Sedan", "SUV"]}

        result = generate_descriptions(class_entities, client)

        assert result["Car"] == "Cars are wheeled vehicles."

    def test_post_process_helper_directly(self):
        """Spot-check the helper for the documented edge cases."""
        assert _post_process_response('"hello"') == "hello"
        assert _post_process_response("  hello  ") == "hello"
        assert _post_process_response('  "hello"  ') == "hello"
        assert _post_process_response("hello") == "hello"
        assert _post_process_response("") == ""
        assert _post_process_response("   ") == ""
        # A single un-paired quote is NOT stripped (the implementation only
        # strips when both ends match).
        assert _post_process_response('"unbalanced') == '"unbalanced'
        assert _post_process_response('unbalanced"') == 'unbalanced"'


class TestPromptLabelCap:
    """Prompt size is bounded by ``MAX_PROMPT_LABELS``.

    Validates: the user prompt is capped at the first ``MAX_PROMPT_LABELS``
    labels in the order received (stable sampler ordering preserved).
    """

    def test_prompt_includes_only_first_max_labels_for_oversized_class(self):
        """For a 50-label class, only the first 30 labels appear in the prompt."""
        client = _ok_client("desc")
        labels = [f"label-{i:03d}" for i in range(50)]
        class_entities = {"Big": labels}

        generate_descriptions(class_entities, client)

        user_prompt = client.generate.call_args.args[0]
        # First 30 labels must all appear, in the order received.
        for i in range(MAX_PROMPT_LABELS):
            assert f"label-{i:03d}" in user_prompt
        # The 31st label (and beyond) must NOT appear.
        for i in range(MAX_PROMPT_LABELS, 50):
            assert f"label-{i:03d}" not in user_prompt

    def test_prompt_preserves_input_order(self):
        """The first 30 labels appear in the same order as the input list."""
        client = _ok_client("desc")
        # Use distinctive labels so substring matching reveals any reordering.
        labels = [f"item-{chr(ord('A') + i)}" for i in range(MAX_PROMPT_LABELS)]
        class_entities = {"Ordered": labels}

        generate_descriptions(class_entities, client)

        user_prompt = client.generate.call_args.args[0]
        # The substring positions of the labels must be strictly increasing.
        positions = [user_prompt.find(label) for label in labels]
        assert all(p >= 0 for p in positions)
        assert positions == sorted(positions)

    def test_build_user_prompt_helper_caps_labels(self):
        """Spot-check the helper directly for the cap constant."""
        labels = [f"x-{i}" for i in range(MAX_PROMPT_LABELS + 5)]
        prompt = _build_user_prompt("ClassA", labels)

        # The label-count line reflects the cap.
        assert f"({MAX_PROMPT_LABELS})" in prompt
        # The first MAX_PROMPT_LABELS labels are present.
        for i in range(MAX_PROMPT_LABELS):
            assert f"x-{i}" in prompt
        # Labels beyond the cap are absent.
        for i in range(MAX_PROMPT_LABELS, MAX_PROMPT_LABELS + 5):
            assert f"x-{i}" not in prompt


class TestFallbackHelperDirect:
    """Direct coverage of ``_fallback_description``.

    Mirrors the public-API tests but exercises the helper directly so a
    regression in formatting fails the smallest possible test.
    """

    def test_zero_labels_format(self):
        assert _fallback_description([]) == "Class representing 0 entities including: "

    def test_below_top_n_lists_all(self):
        assert _fallback_description(["b", "a"]) == "Class representing 2 entities including: a, b"

    def test_above_top_n_truncates(self):
        labels = ["g", "f", "e", "d", "c", "b", "a"]
        assert _fallback_description(labels) == "Class representing 7 entities including: a, b, c, d, e"


# ── Enriched-context mode ───────────────────────────────────────────────


from coa_ontology.inducer.unstructured.services.description_generator import (  # noqa: E402
    MAX_FACTS_PER_ENTITY,
    MAX_FACTS_PER_PROMPT,
    MAX_PROMPT_ENTITIES,
    EntityContext,
    _build_enriched_user_prompt,
    generate_descriptions_with_context,
)


class TestEnrichedSuccessPath:
    """Successful enriched description generation.

    The enriched API takes ``EntityContext`` records (label + facts) and
    feeds both into the LLM prompt. Tests verify the LLM call, prompt
    construction, and round-trip behaviour mirror the label-only path
    where applicable.
    """

    def test_returns_llm_response_as_description(self):
        client = _ok_client("Foundry partners produce semiconductor wafers.")
        contexts = {
            "Company": [
                EntityContext(
                    label="foundry partners",
                    facts=[
                        "foundry partners TYPE OF suppliers",
                        "foundry partners SERVES customers",
                    ],
                ),
            ],
        }

        result = generate_descriptions_with_context(contexts, client)

        assert result == {
            "Company": "Foundry partners produce semiconductor wafers.",
        }

    def test_llm_called_with_enriched_system_and_user_prompts(self):
        """System prompt should be the enriched variant; user prompt non-empty."""
        client = _ok_client("desc")
        contexts = {"Company": [EntityContext(label="acme", facts=["acme MAKES widgets"])]}

        generate_descriptions_with_context(contexts, client)

        assert client.generate.call_count == 1
        call = client.generate.call_args
        user_prompt = call.args[0]
        system_prompt = call.kwargs["system"]

        # User prompt mentions the class label, the entity label, and the fact.
        assert "Company" in user_prompt
        assert "acme" in user_prompt
        assert "MAKES widgets" in user_prompt
        # System prompt is the enriched variant — distinct from the
        # label-only one.
        assert "facts" in system_prompt.lower()
        assert "ground" in system_prompt.lower()

    def test_one_llm_call_per_class(self):
        client = _ok_client("desc")
        contexts = {
            "Company": [EntityContext(label="acme", facts=[])],
            "Person": [EntityContext(label="alice", facts=[])],
        }

        result = generate_descriptions_with_context(contexts, client)

        assert client.generate.call_count == 2
        assert set(result.keys()) == {"Company", "Person"}


class TestEnrichedFallback:
    """Fallback semantics for the enriched API.

    The fallback uses entity LABELS only (drops facts) because its
    purpose is to be deterministic and brief, not informative.
    """

    def test_runtime_error_falls_back_to_labels(self):
        client = _failing_client(RuntimeError("boom"))
        contexts = {
            "Company": [
                EntityContext(label="acme", facts=["acme MAKES widgets"]),
                EntityContext(label="globex", facts=[]),
            ]
        }

        result = generate_descriptions_with_context(contexts, client)

        # Fallback format is "Class representing N entities including: <labels>".
        # N is 2 (entity count, not fact count).
        text = result["Company"]
        assert text.startswith("Class representing 2 entities including:")
        assert "acme" in text
        assert "globex" in text
        # Facts are NOT included in the fallback.
        assert "widgets" not in text

    def test_empty_response_falls_back(self):
        client = _ok_client("")
        contexts = {"Company": [EntityContext(label="acme", facts=[])]}

        result = generate_descriptions_with_context(contexts, client)
        assert result["Company"].startswith("Class representing 1 entities including:")

    def test_empty_contexts_for_class_uses_fallback_without_llm_call(self):
        client = _ok_client("should not be used")
        contexts = {"Empty": []}

        result = generate_descriptions_with_context(contexts, client)

        assert result == {"Empty": "Class representing 0 entities including: "}
        client.generate.assert_not_called()

    def test_empty_input_dict_returns_empty(self):
        client = _ok_client("desc")
        assert generate_descriptions_with_context({}, client) == {}
        client.generate.assert_not_called()


class TestEnrichedPromptCaps:
    """Per-entity and global caps on prompt size.

    Verifies that the enriched prompt builder respects
    ``MAX_PROMPT_ENTITIES``, ``MAX_FACTS_PER_ENTITY``, and the global
    ``MAX_FACTS_PER_PROMPT`` budget.
    """

    def test_entity_count_capped(self):
        # MAX_PROMPT_ENTITIES + 5 entities; only first MAX_PROMPT_ENTITIES
        # should appear in the prompt.
        contexts = [EntityContext(label=f"entity-{i:03d}", facts=[]) for i in range(MAX_PROMPT_ENTITIES + 5)]
        prompt = _build_enriched_user_prompt("ClassA", contexts)

        for i in range(MAX_PROMPT_ENTITIES):
            assert f"entity-{i:03d}" in prompt
        for i in range(MAX_PROMPT_ENTITIES, MAX_PROMPT_ENTITIES + 5):
            assert f"entity-{i:03d}" not in prompt

    def test_facts_per_entity_capped(self):
        # One entity with many facts: only MAX_FACTS_PER_ENTITY should appear.
        facts = [f"acme FACT {i:03d}" for i in range(MAX_FACTS_PER_ENTITY + 5)]
        prompt = _build_enriched_user_prompt(
            "Company",
            [EntityContext(label="acme", facts=facts)],
        )

        for i in range(MAX_FACTS_PER_ENTITY):
            assert f"FACT {i:03d}" in prompt
        for i in range(MAX_FACTS_PER_ENTITY, MAX_FACTS_PER_ENTITY + 5):
            assert f"FACT {i:03d}" not in prompt

    def test_global_facts_budget_enforced(self):
        # MAX_PROMPT_ENTITIES entities each with MAX_FACTS_PER_ENTITY facts
        # would exceed MAX_FACTS_PER_PROMPT. Verify the global cap kicks in.
        per_entity_facts = MAX_FACTS_PER_ENTITY
        contexts = [
            EntityContext(
                label=f"e{i}",
                facts=[f"e{i} FACT {j:03d}" for j in range(per_entity_facts)],
            )
            for i in range(MAX_PROMPT_ENTITIES)
        ]
        prompt = _build_enriched_user_prompt("Class", contexts)

        # Count ALL bullet-fact lines in the prompt.
        fact_lines = [line for line in prompt.splitlines() if line.lstrip().startswith("·")]
        assert len(fact_lines) <= MAX_FACTS_PER_PROMPT

    def test_empty_facts_entity_still_appears(self):
        """An entity with zero facts should still be listed as a label."""
        contexts = [EntityContext(label="acme", facts=[])]
        prompt = _build_enriched_user_prompt("Class", contexts)
        assert "acme" in prompt


class TestEnrichedFaultIsolation:
    """Per-class failures must not abort the enriched batch."""

    def test_one_failure_one_success_yields_both_entries(self):
        client = MagicMock()
        client.generate.side_effect = [
            RuntimeError("first call fails"),
            "Persons are people.",
        ]
        contexts = {
            "Company": [EntityContext(label="acme", facts=["acme MAKES widgets"])],
            "Person": [EntityContext(label="alice", facts=["alice WORKS AT acme"])],
        }

        result = generate_descriptions_with_context(contexts, client)

        assert set(result.keys()) == {"Company", "Person"}
        assert result["Company"].startswith("Class representing 1 entities including:")
        assert result["Person"] == "Persons are people."


class TestEnrichedResponsePostProcessing:
    """The enriched API uses the same post-processing as the label-only API."""

    def test_surrounding_double_quotes_stripped(self):
        client = _ok_client('"Companies operate factories."')
        contexts = {"Company": [EntityContext(label="acme", facts=["acme MAKES widgets"])]}
        result = generate_descriptions_with_context(contexts, client)
        assert result["Company"] == "Companies operate factories."


# ── Schema-context mode ─────────────────────────────────────────────────


from coa_ontology.inducer.unstructured.services.description_generator import (  # noqa: E402
    MAX_SCHEMA_EDGES_PER_DIRECTION,
    ClassSchemaContext,
    SchemaEdge,
    _build_schema_user_prompt,
    _schema_fallback_description,
    generate_descriptions_with_schema,
)


def _ctx(
    label: str,
    outgoing: list[tuple[str, str, int]] | None = None,
    incoming: list[tuple[str, str, int]] | None = None,
) -> ClassSchemaContext:
    """Test helper: build a ClassSchemaContext from compact tuple lists."""
    out_edges = [SchemaEdge(predicate=p, other_class=c, frequency=f) for (p, c, f) in (outgoing or [])]
    in_edges = [SchemaEdge(predicate=p, other_class=c, frequency=f) for (p, c, f) in (incoming or [])]
    return ClassSchemaContext(
        class_label=label,
        outgoing=out_edges,
        incoming=in_edges,
    )


class TestSchemaSuccessPath:
    """Successful schema-mode description generation."""

    def test_returns_llm_response_as_description(self):
        client = _ok_client("A Company is connected to suppliers and customers via operates_in and relies_on edges.")
        schemas = {
            "Company": _ctx(
                "Company",
                outgoing=[
                    ("operates_in", "Location", 42),
                    ("relies_on", "Software", 31),
                ],
                incoming=[("works_for", "Person", 18)],
            )
        }

        result = generate_descriptions_with_schema(schemas, client)

        assert result == {
            "Company": ("A Company is connected to suppliers and customers via operates_in and relies_on edges.")
        }

    def test_llm_called_with_schema_system_and_user_prompt(self):
        """System prompt should be the schema variant; user prompt non-empty."""
        client = _ok_client("desc")
        schemas = {
            "Company": _ctx(
                "Company",
                outgoing=[("operates_in", "Location", 5)],
            )
        }

        generate_descriptions_with_schema(schemas, client)

        assert client.generate.call_count == 1
        call = client.generate.call_args
        user_prompt = call.args[0]
        system_prompt = call.kwargs["system"]

        # User prompt mentions the class, the predicate, and the target.
        assert "Company" in user_prompt
        assert "operates_in" in user_prompt
        assert "Location" in user_prompt
        # System prompt is the schema variant — distinct from instance/label.
        assert "TYPE-LEVEL" in system_prompt
        # Should explicitly tell the LLM not to use instance values.
        assert "specific instance values" in system_prompt.lower()

    def test_one_llm_call_per_class(self):
        client = _ok_client("desc")
        schemas = {
            "Company": _ctx("Company", outgoing=[("p", "X", 1)]),
            "Person": _ctx("Person", incoming=[("q", "Y", 1)]),
        }
        result = generate_descriptions_with_schema(schemas, client)

        assert client.generate.call_count == 2
        assert set(result.keys()) == {"Company", "Person"}


class TestSchemaPromptShape:
    """Direct tests on the prompt builder."""

    def test_outgoing_section_includes_arrow_and_frequency(self):
        ctx = _ctx(
            "Company",
            outgoing=[("operates_in", "Location", 42)],
        )
        prompt = _build_schema_user_prompt(ctx)
        assert "Outgoing relationships" in prompt
        assert "Company --operates_in--> Location" in prompt
        assert "frequency 42" in prompt

    def test_incoming_section_uses_other_class_as_source(self):
        ctx = _ctx(
            "Company",
            incoming=[("works_for", "Person", 18)],
        )
        prompt = _build_schema_user_prompt(ctx)
        assert "Incoming relationships" in prompt
        # Incoming arrow goes other_class --pred--> this class.
        assert "Person --works_for--> Company" in prompt
        assert "frequency 18" in prompt

    def test_empty_directions_marked_as_none_observed(self):
        ctx = _ctx("Company")
        prompt = _build_schema_user_prompt(ctx)
        assert "Outgoing relationships: (none observed)" in prompt
        assert "Incoming relationships: (none observed)" in prompt

    def test_edge_count_capped_per_direction(self):
        # Build more than the cap of outgoing edges.
        outgoing = [(f"pred_{i}", "X", 100 - i) for i in range(MAX_SCHEMA_EDGES_PER_DIRECTION + 5)]
        ctx = _ctx("C", outgoing=outgoing)
        prompt = _build_schema_user_prompt(ctx)

        # First MAX_SCHEMA_EDGES_PER_DIRECTION present.
        for i in range(MAX_SCHEMA_EDGES_PER_DIRECTION):
            assert f"pred_{i}" in prompt
        # Beyond the cap excluded.
        for i in range(MAX_SCHEMA_EDGES_PER_DIRECTION, MAX_SCHEMA_EDGES_PER_DIRECTION + 5):
            assert f"pred_{i}" not in prompt


class TestSchemaFallback:
    """Fallback semantics for the schema mode."""

    def test_no_edges_uses_fallback_without_llm_call(self):
        client = _ok_client("should not be used")
        schemas = {"Empty": _ctx("Empty")}

        result = generate_descriptions_with_schema(schemas, client)

        assert result["Empty"].startswith("Class 'Empty' with no observed")
        client.generate.assert_not_called()

    def test_runtime_error_falls_back_to_summary(self):
        client = _failing_client(RuntimeError("boom"))
        schemas = {
            "Company": _ctx(
                "Company",
                outgoing=[
                    ("operates_in", "Location", 10),
                    ("relies_on", "Software", 5),
                ],
            )
        }

        result = generate_descriptions_with_schema(schemas, client)

        text = result["Company"]
        assert text.startswith("Class 'Company'")
        assert "operates_in" in text
        assert "relies_on" in text

    def test_empty_response_falls_back(self):
        client = _ok_client("")
        schemas = {
            "Company": _ctx(
                "Company",
                outgoing=[("operates_in", "Location", 10)],
            )
        }
        result = generate_descriptions_with_schema(schemas, client)
        assert result["Company"].startswith("Class 'Company'")

    def test_fallback_includes_unique_predicates_only(self):
        """Fallback summary should de-duplicate predicates across edges."""
        ctx = _ctx(
            "Company",
            outgoing=[
                ("operates_in", "Location", 10),
                ("operates_in", "Country", 8),  # same predicate, different target
                ("relies_on", "Software", 5),
            ],
        )
        text = _schema_fallback_description(ctx)
        # "operates_in" should appear once even though two edges use it.
        assert text.count("operates_in") == 1
        assert "relies_on" in text


class TestSchemaFaultIsolation:
    """A failure on one class doesn't abort the schema-mode batch."""

    def test_one_failure_one_success(self):
        client = MagicMock()
        client.generate.side_effect = [
            RuntimeError("first call fails"),
            "Persons participate in employment and ownership relationships.",
        ]
        schemas = {
            "Company": _ctx("Company", outgoing=[("p", "X", 5)]),
            "Person": _ctx("Person", outgoing=[("q", "Y", 3)]),
        }

        result = generate_descriptions_with_schema(schemas, client)

        assert set(result.keys()) == {"Company", "Person"}
        assert result["Company"].startswith("Class 'Company'")
        assert result["Person"].startswith("Persons participate")


class TestSchemaResponsePostProcessing:
    """The schema API uses the same post-processing as label-only / enriched."""

    def test_surrounding_double_quotes_stripped(self):
        client = _ok_client('"Companies operate locations."')
        schemas = {"Company": _ctx("Company", outgoing=[("op", "Location", 5)])}
        result = generate_descriptions_with_schema(schemas, client)
        assert result["Company"] == "Companies operate locations."
