# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the unstructured induction pipeline orchestrator.

Validates :class:`UnstructuredInductionPipeline.run` end-to-end at the
function-call level with every external dependency mocked. The
underlying induction-rule services (``induce_classes``,
``induce_object_properties`` etc.) run for real against the mocked
data so the test exercises the full call-shape sequence and the
graph-derived counts on :class:`UnstructuredInductionReport` — not
only the mock-call assertions.

The orchestrator emits stage transitions via ``on_status`` BEFORE the
matching stage's work begins. This file's ``StageRecorder`` callback
captures the exact emission order so the documented progression can
be asserted against the orchestrator's actual behavior.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import GRAPH_BASE_URI

pytestmark = pytest.mark.unit

from coa_control_plane_server.models.induction_job_status import (
    InductionJobStatus,
)
from coa_ontology.bedrock_embeddings import BedrockEmbeddingClient
from coa_ontology.inducer.unstructured.schemas import (
    SamplingParams,
    UnstructuredInductionConfig,
)
from coa_ontology.inducer.unstructured.services.description_generator import (
    LLMDescriptionClient,
)
from coa_ontology.inducer.unstructured.services.iri_resolver import (
    InMemoryIRIResolver,
)
from coa_ontology.inducer.unstructured.services.pipeline import (
    V0_WARNINGS,
    UnstructuredInductionPipeline,
    _build_embedding_text,
    _build_grounding_inputs,
)
from coa_ontology.inducer.unstructured.stores.protocol import (
    ClassRecord,
    ClassSchemaResult,
    EntityRecord,
    FactRecord,
    LexicalGraphStore,
    RelationRecord,
    TopicRecord,
)
from rdflib import OWL, RDF, Graph

_NS = "test-ns"
_NAME = "test-onto"
_ONTOLOGY_URI_PREFIX = f"{GRAPH_BASE_URI}/namespace/{_NS}/ontology/{_NAME}"
_GRAPH_ARN = "arn:aws:neptune-graph:us-east-1:000000000000:graph/g-test"

# A small, deterministic class distribution. The CamelCase values are
# exactly what ``LexicalGraphStore.get_graph_schema`` would return; the
# orchestrator's sampler maps them to spaced labels (e.g.
# "FinancialInstrument" → "Financial Instrument") via ``_camel_to_spaced``.
_CLASSES: list[ClassRecord] = [
    ClassRecord(value="Company", count=10),
    ClassRecord(value="Person", count=8),
    ClassRecord(value="FinancialInstrument", count=5),
]

# Relations between the three classes feed induce_object_properties.
_RELATIONS: list[RelationRecord] = [
    RelationRecord(source_class="Company", predicate="employs", target_class="Person", count=12),
    RelationRecord(source_class="Person", predicate="works_for", target_class="Company", count=12),
    RelationRecord(source_class="Company", predicate="issues", target_class="FinancialInstrument", count=4),
    RelationRecord(source_class="Person", predicate="owns", target_class="FinancialInstrument", count=3),
    RelationRecord(source_class="Company", predicate="competes_with", target_class="Company", count=2),
]


def _entities_for(classification: str) -> list[EntityRecord]:
    """Return a small, stable list of entity records per spaced classification.

    The orchestrator calls ``get_entities_by_class`` twice in the
    happy path: first inside ``stratified_sample`` (to pick IDs to
    sample), then inside ``_resolve_sampled_entities`` (to look up the
    surface labels for those IDs). Returning the same list each time
    keeps both call sites consistent.
    """
    if classification == "Company":
        return [
            EntityRecord(entity_id="c1", value="Apple", classification="Company"),
            EntityRecord(entity_id="c2", value="Globex", classification="Company"),
            EntityRecord(entity_id="c3", value="Initech", classification="Company"),
            EntityRecord(entity_id="c4", value="Acme", classification="Company"),
            EntityRecord(entity_id="c5", value="Hooli", classification="Company"),
        ]
    if classification == "Person":
        return [
            EntityRecord(entity_id="p1", value="Alice", classification="Person"),
            EntityRecord(entity_id="p2", value="Bob", classification="Person"),
            EntityRecord(entity_id="p3", value="Carol", classification="Person"),
            EntityRecord(entity_id="p4", value="Dave", classification="Person"),
        ]
    if classification == "Financial Instrument":
        return [
            EntityRecord(entity_id="f1", value="Treasury Bond", classification="Financial Instrument"),
            EntityRecord(entity_id="f2", value="Corporate Note", classification="Financial Instrument"),
            EntityRecord(entity_id="f3", value="Commercial Paper", classification="Financial Instrument"),
        ]
    return []


def _facts_for(entity_ids: list[str]) -> list[FactRecord]:
    """Return a small set of SPO facts among the test entities.

    Limited to one fact per (subject_class, predicate) so Rule 3
    (``induce_object_properties``) emits a deterministic number of
    ``owl:ObjectProperty`` declarations downstream. Returns an empty
    list when no entity IDs were sampled (a defensive guard against a
    sampler-skipped class accidentally producing facts).
    """
    if not entity_ids:
        return []
    fact_specs = [
        ("Apple", "Company", "employs", "Alice", "Person", None),
        ("Globex", "Company", "issues", "Treasury Bond", "Financial Instrument", None),
        ("Alice", "Person", "works_for", "Apple", "Company", None),
    ]
    return [
        FactRecord(
            subject_value=sv,
            subject_class=sc,
            predicate=pred,
            object_value=ov,
            object_class=oc,
            complement=co,
        )
        for sv, sc, pred, ov, oc, co in fact_specs
    ]


def _topics() -> list[TopicRecord]:
    """Two simple topics so Rule 5 emits a non-zero ``conceptCount``."""
    return [
        TopicRecord(topic_id="t1", value="finance", statement_count=3),
        TopicRecord(topic_id="t2", value="technology", statement_count=2),
    ]


def _make_lexical_store() -> MagicMock:
    """Build a :class:`LexicalGraphStore` mock returning the test data.

    Uses ``spec=LexicalGraphStore`` so ``isinstance`` checks performed
    by Pydantic against the runtime-checkable protocol succeed. Side
    effects on ``get_entities_by_class`` and ``get_facts_for_entities``
    are routed through helpers so each call returns the same data
    regardless of invocation order.
    """
    store = MagicMock(spec=LexicalGraphStore)
    store.get_graph_schema.return_value = ClassSchemaResult(
        classes=list(_CLASSES),
        relations=list(_RELATIONS),
    )
    store.get_entities_by_class.side_effect = lambda classification, limit: _entities_for(classification)
    store.get_facts_for_entities.side_effect = lambda entity_ids: _facts_for(entity_ids)
    store.get_topics.return_value = _topics()
    return store


def _make_description_llm() -> MagicMock:
    """LLM client mock returning canned descriptions.

    The orchestrator's schema-mode path calls
    ``description_llm.generate(prompt, system, max_tokens)`` once per
    class. Returning a different short string per call keeps the
    descriptions distinct (so the embedding client gets distinct
    embed-text inputs) without making the test depend on prompt
    contents.
    """
    llm = MagicMock(spec=LLMDescriptionClient)
    canned = [
        "A business organization that produces goods or services.",
        "A human individual.",
        "A tradable financial asset such as a bond or note.",
    ]
    llm.generate.side_effect = canned + ["A generated description."] * 100
    return llm


class _DistinctOrthogonalEmbedder:
    """Returns a distinct unit-orthogonal vector for every unique input.

    The class normalizer's match band is driven by cosine similarity
    against stored vectors, so embedding collisions silently merge
    classes (both are flagged ``exact``, the second class reuses the
    first's IRI, and the merged class winds up with multiple labels).
    Test input order should not change which classes get merged — and
    in fact the test's intent is that NONE of them merge — so we hand
    out a freshly-orthogonal one-hot per distinct input, growing the
    vector dimension as needed.
    """

    def __init__(self) -> None:
        self._slots: dict[str, int] = {}

    def __call__(self, text: str) -> list[float]:
        slot = self._slots.setdefault(text, len(self._slots))
        # Pad to (slot+1) and place a 1.0 at that slot. Vectors stay
        # unit-length and pairwise-orthogonal across distinct inputs,
        # so cosine between any two distinct inputs is exactly 0.0
        # (which falls into the normalizer's ``novel`` band).
        vec = [0.0] * (slot + 1)
        vec[slot] = 1.0
        return vec


def _make_embedding_client() -> MagicMock:
    """Embedding client mock returning deterministic orthogonal unit vectors."""
    client = MagicMock(spec=BedrockEmbeddingClient)
    client.embed_text.side_effect = _DistinctOrthogonalEmbedder()
    # model_id is an instance attribute (set in __init__), so spec= does not
    # expose it — set it explicitly. The pipeline passes it through to the
    # grounding service as the recall model id.
    client.model_id = "titan-embed-test"
    return client


def _make_iri_resolver() -> InMemoryIRIResolver:
    """Real :class:`InMemoryIRIResolver`."""
    return InMemoryIRIResolver(namespace=_NS, ontology_name=_NAME)


def _make_config(
    *,
    description_mode: str = "schema",
    include_individuals: bool = False,
    entailment_enabled: bool = False,
    sampling: SamplingParams | None = None,
    grounding_service=None,
    grounding_ontology_ids: list[str] | None = None,
    grounding_mode: str = "ENHANCED",
) -> UnstructuredInductionConfig:
    """Build a fully-populated :class:`UnstructuredInductionConfig`.

    All four DI clients (``lexical_store``, ``embedding_client``,
    ``description_llm``, ``iri_resolver``) are constructed via the
    mocking helpers above so the config validates under
    ``arbitrary_types_allowed=True``. ``grounding_*`` default to the
    disabled state.
    """
    return UnstructuredInductionConfig(
        namespace=_NS,
        name=_NAME,
        ontology_uri_prefix=_ONTOLOGY_URI_PREFIX,
        graph_arn=_GRAPH_ARN,
        confidence_threshold=0.80,
        sampling=sampling or SamplingParams(k_min=5, floor=3),
        description_mode=description_mode,  # type: ignore[arg-type]
        include_individuals=include_individuals,
        entailment_enabled=entailment_enabled,
        grounding_ontology_ids=grounding_ontology_ids,
        grounding_mode=grounding_mode,
        lexical_store=_make_lexical_store(),
        embedding_client=_make_embedding_client(),
        description_llm=_make_description_llm(),
        iri_resolver=_make_iri_resolver(),
        grounding_service=grounding_service,
    )


class StageRecorder:
    """Callable that records every ``on_status`` invocation in order.

    The orchestrator emits ``on_status(stage)`` BEFORE each stage
    starts. Capturing the call sequence lets the
    tests assert the documented progression matches the
    implementation — and that the FAILED case actually fires
    ``on_status(FAILED)`` before the exception propagates.
    """

    def __init__(self) -> None:
        self.stages: list[InductionJobStatus] = []

    def __call__(self, stage: InductionJobStatus) -> None:
        self.stages.append(stage)


_EXPECTED_HAPPY_PATH_STAGES = [
    InductionJobStatus.QUERYING_NEPTUNE,
    InductionJobStatus.SAMPLING,
    InductionJobStatus.GENERATING_DESCRIPTIONS,
    InductionJobStatus.RESOLVING_IRIS,
    InductionJobStatus.APPLYING_RULES,
    InductionJobStatus.ENTAILMENT,
    InductionJobStatus.SERIALIZING,
]


class TestStageProgression:
    """Verifies the orchestrator emits the documented stage sequence."""

    def test_records_exact_stage_sequence(self):
        """All seven stages emit exactly once in the documented order.

        STORING / COMPLETED are deliberately NOT emitted by the
        orchestrator (those are owned by the worker thread per the
        design doc). The orchestrator's contract stops at SERIALIZING.
        """
        recorder = StageRecorder()
        config = _make_config()

        UnstructuredInductionPipeline().run(config, on_status=recorder)

        assert recorder.stages == _EXPECTED_HAPPY_PATH_STAGES

    def test_default_callback_does_not_raise(self):
        """``on_status=None`` defaults to a no-op without breaking the run."""
        config = _make_config()
        # No on_status argument — the orchestrator must still run end-to-end.
        report = UnstructuredInductionPipeline().run(config)
        assert report.tboxClassCount >= 1

    def test_querying_neptune_emitted_before_get_graph_schema(self):
        """Stage emission happens BEFORE the matching work begins.

        We capture the relative order between the ``on_status``
        callback and the lexical store's ``get_graph_schema`` call by
        recording both into a single shared list — confirming the
        emit-before-work invariant.
        """
        events: list[str] = []
        config = _make_config()
        # Wrap the existing mocked side-effect: when called, log the
        # event and then invoke the configured side effect.
        original = config.lexical_store.get_graph_schema.return_value

        def record_then_return():
            events.append("get_graph_schema")
            return original

        config.lexical_store.get_graph_schema.side_effect = record_then_return

        def recorder(stage: InductionJobStatus) -> None:
            events.append(f"emit:{stage.value}")

        UnstructuredInductionPipeline().run(config, on_status=recorder)

        # The QUERYING_NEPTUNE emission MUST appear before the
        # ``get_graph_schema`` call in the recorded event sequence.
        emit_idx = events.index(f"emit:{InductionJobStatus.QUERYING_NEPTUNE.value}")
        work_idx = events.index("get_graph_schema")
        assert emit_idx < work_idx, (
            f"Expected QUERYING_NEPTUNE to be emitted before get_graph_schema; got events={events!r}"
        )


class TestReportContents:
    """The :class:`UnstructuredInductionReport` carries the documented fields."""

    def test_class_count_matches_input_distribution(self):
        """``tboxClassCount`` reflects the classes that passed the floor.

        All three test classes have count >= floor=3, so all three are
        induced. The orchestrator counts subjects of ``rdf:type
        owl:Class`` in the merged graph.
        """
        config = _make_config()
        report = UnstructuredInductionPipeline().run(config)
        # 3 classes in the test distribution, all above the default floor.
        assert report.tboxClassCount == 3

    def test_subclass_axiom_count_is_zero_in_v0(self):
        """The axiom counts are zero after a real run."""
        config = _make_config()
        report = UnstructuredInductionPipeline().run(config)
        assert report.subclassAxiomCount == 0
        assert report.equivalenceAxiomCount == 0
        assert report.disjointnessAxiomCount == 0

    def test_description_mode_round_trips_to_report(self):
        """The configured ``description_mode`` appears verbatim on the report."""
        config = _make_config(description_mode="schema")
        report = UnstructuredInductionPipeline().run(config)
        assert report.description_mode == "schema"

    def test_namespace_and_ontology_id_round_trip(self):
        """``namespace`` and ``ontology_id`` carry the config values."""
        config = _make_config()
        report = UnstructuredInductionPipeline().run(config)
        assert report.namespace == _NS
        assert report.ontology_id == _NAME

    def test_pipeline_run_id_is_a_nonempty_string(self):
        """``pipeline_run_id`` is generated per run (uuid)."""
        config = _make_config()
        report = UnstructuredInductionPipeline().run(config)
        assert isinstance(report.pipeline_run_id, str)
        assert report.pipeline_run_id  # non-empty

    def test_started_and_completed_timestamps_set(self):
        """Both timestamps are set on a successful run."""
        config = _make_config()
        report = UnstructuredInductionPipeline().run(config)
        assert report.started_at is not None
        assert report.completed_at is not None
        assert report.completed_at >= report.started_at

    def test_turtle_is_a_nonempty_string(self):
        """The serializer's Turtle output is included on the report."""
        config = _make_config()
        report = UnstructuredInductionPipeline().run(config)
        g = Graph().parse(data=report.turtle, format="turtle")
        assert len(set(g.subjects(RDF.type, OWL.Class))) == 3


class TestV0Warnings:
    """The report's ``warnings`` list surfaces the v0 limitation strings."""

    def test_three_v0_warnings_present_by_default(self):
        """A baseline run emits exactly the three documented v0 warnings."""
        config = _make_config()
        report = UnstructuredInductionPipeline().run(config)

        # The three v0 limitation strings are pre-populated on every run.
        for expected in V0_WARNINGS:
            assert expected in report.warnings, (
                f"Expected v0 warning {expected!r} to be present in report.warnings but got: {report.warnings!r}"
            )

    def test_v0_warnings_match_documented_constants(self):
        """The pre-populated warnings match the module-level ``V0_WARNINGS``.

        Locks in the specific strings so a future edit to the warning
        text trips a failing test instead of silently changing the API contract.
        """
        expected = {
            "entailment stage is a pass-through in v0",
            "datatype property output may be sparse — see parent spec Task 12B",
            "class embedding index is rebuilt per run — see parent spec Task 10A",
        }
        assert set(V0_WARNINGS) == expected


class TestFailureHandling:
    """An exception inside a stage emits FAILED and propagates intact."""

    def test_induce_classes_exception_emits_failed_and_propagates(self):
        """Patching ``induce_classes`` to raise causes FAILED + reraise.

        The patch target is the orchestrator's import binding (NOT the
        original module) — that's where the orchestrator looks up the
        symbol at call time, so patching there is the only way to
        intercept the call.
        """
        recorder = StageRecorder()
        config = _make_config()

        sentinel = RuntimeError("rule 1 failed deliberately")

        with (
            patch(
                "coa_ontology.inducer.unstructured.services.pipeline.induce_classes",
                side_effect=sentinel,
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            UnstructuredInductionPipeline().run(config, on_status=recorder)

        # The exact same exception object propagates — confirms the
        # orchestrator's bare ``raise`` (not a re-wrap) is in effect.
        assert exc_info.value is sentinel

        # FAILED is the LAST emission and replaces the would-have-been
        # ENTAILMENT / SERIALIZING tail.
        assert recorder.stages[-1] == InductionJobStatus.FAILED

        # All four pre-failure stages emitted in order.
        assert recorder.stages[:4] == _EXPECTED_HAPPY_PATH_STAGES[:4]

    def test_failed_emits_only_once(self):
        """``on_status(FAILED)`` fires exactly once on a single-stage error."""
        recorder = StageRecorder()
        config = _make_config()

        with (
            patch(
                "coa_ontology.inducer.unstructured.services.pipeline.induce_classes",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(RuntimeError),
        ):
            UnstructuredInductionPipeline().run(config, on_status=recorder)

        assert recorder.stages.count(InductionJobStatus.FAILED) == 1


class TestEntailmentEnabledForwardCompat:
    """``entailment_enabled=True`` warns but otherwise behaves identically."""

    def test_entailment_enabled_appends_warning(self):
        """The "not yet implemented" warning is appended to ``warnings``."""
        config = _make_config(entailment_enabled=True)
        report = UnstructuredInductionPipeline().run(config)

        # Plus the three v0 baseline warnings.
        for expected in V0_WARNINGS:
            assert expected in report.warnings

        # Plus the entailment-specific forward-compat warning.
        assert "entailment_enabled=True is not yet implemented; treating as False" in report.warnings

    def test_entailment_enabled_does_not_change_stage_sequence(self):
        """The same seven stages emit regardless of ``entailment_enabled``."""
        recorder_enabled = StageRecorder()
        recorder_disabled = StageRecorder()

        UnstructuredInductionPipeline().run(_make_config(entailment_enabled=True), on_status=recorder_enabled)
        UnstructuredInductionPipeline().run(_make_config(entailment_enabled=False), on_status=recorder_disabled)

        assert recorder_enabled.stages == recorder_disabled.stages == _EXPECTED_HAPPY_PATH_STAGES

    def test_entailment_enabled_does_not_change_axiom_counts(self):
        """Subclass / equivalence / disjointness counts stay 0."""
        report = UnstructuredInductionPipeline().run(_make_config(entailment_enabled=True))
        assert report.subclassAxiomCount == 0
        assert report.equivalenceAxiomCount == 0
        assert report.disjointnessAxiomCount == 0


class TestIncludeIndividualsGate:
    """``include_individuals=False`` produces zero NamedIndividual declarations."""

    def test_default_yields_zero_individual_count(self):
        """Default (``include_individuals=False``) skips Rule 2 entirely."""
        report = UnstructuredInductionPipeline().run(_make_config())
        assert report.individualCount == 0

    def test_no_owl_named_individual_in_turtle_when_disabled(self):
        """The serialized Turtle has no ``owl:NamedIndividual`` declarations.

        The Turtle output is a stronger check than just the count
        field: it confirms zero declarations leaked into the
        graph regardless of how the count was derived.
        """
        report = UnstructuredInductionPipeline().run(_make_config(include_individuals=False))
        # Per ``serializer.serialize``, prefix bindings give us
        # ``owl:NamedIndividual`` shorthand. Either the prefixed form
        # OR the full IRI form would identify a leaked declaration.
        assert "owl:NamedIndividual" not in report.turtle
        assert "http://www.w3.org/2002/07/owl#NamedIndividual" not in report.turtle

    def test_include_individuals_true_emits_individuals(self):
        """``include_individuals=True`` produces non-zero ``individualCount``.

        Sanity-check counterweight to the gate-off tests: confirms the
        count field is genuinely tied to Rule 2's output, so the
        zero-result above is meaningful (not a defaulted-zero artifact).
        """
        report = UnstructuredInductionPipeline().run(_make_config(include_individuals=True))
        assert report.individualCount > 0


def _grounding_result_for(label: str):
    """Fake GroundingResult: ground 'Financial Instrument' to a FIBO class,
    everything else novel. Keyed by the spaced class label the pipeline
    passes as ``class_label``."""
    from coa_ontology.inducer.services.grounding import (
        GroundingCandidate,
        GroundingResult,
    )

    if label == "Financial Instrument":
        chosen = GroundingCandidate(
            entity_uri="http://fibo/FinancialInstrument",
            ontology_id="fibo",
            label="FinancialInstrument",
            definition="A tradable financial asset.",
            lexical_sim=0.96,
        )
        return GroundingResult(
            source_table=label,
            source_column="",
            candidates=[chosen],
            chosen=chosen,
            match_type="exact",
            relationship="exactMatch",
            confidence=0.96,
            reason="exact",
            mode="ENHANCED",
        )
    return GroundingResult(
        source_table=label,
        source_column="",
        candidates=[],
        chosen=None,
        match_type="novel",
        relationship=None,
        confidence=None,
        reason="novel",
        mode="ENHANCED",
    )


def _make_grounding_service() -> MagicMock:
    from coa_ontology.inducer.services.grounding import GroundingService

    # spec= so Pydantic's isinstance(GroundingService) field check passes.
    svc = MagicMock(spec=GroundingService)
    svc.ground_class.side_effect = lambda *, class_label, **kwargs: _grounding_result_for(class_label)
    # to_concept_match is a pure GroundingResult→ConceptMatch mapping; delegate
    # to the REAL implementation so the pipeline's `.model_dump()` on it works
    # (a bare MagicMock would fail Pydantic serialization).
    svc.to_concept_match.side_effect = lambda result: GroundingService.to_concept_match(svc, result)
    return svc


class TestFoundationalGrounding:
    """The pipeline grounds induced classes against loaded
    foundational ontologies when a grounding service + ontology ids are
    configured, and is a pure no-op otherwise."""

    def test_disabled_by_default_no_grounding_axioms(self):
        report = UnstructuredInductionPipeline().run(_make_config())
        assert report.foundationalGroundedCount == 0
        # No rdfs:subClassOf to an external FIBO class in the Turtle.
        assert "http://fibo/" not in report.turtle

    def test_service_present_but_no_ontology_ids_is_noop(self):
        svc = _make_grounding_service()
        report = UnstructuredInductionPipeline().run(_make_config(grounding_service=svc, grounding_ontology_ids=None))
        assert report.foundationalGroundedCount == 0
        svc.ground_class.assert_not_called()

    def test_grounds_classes_against_loaded_ontology(self):
        svc = _make_grounding_service()
        report = UnstructuredInductionPipeline().run(
            _make_config(grounding_service=svc, grounding_ontology_ids=["fibo"])
        )
        # Exactly one class ('Financial Instrument') grounds to FIBO.
        assert report.foundationalGroundedCount == 1
        # The grounding axioms are present in the serialized Turtle.
        assert "http://fibo/FinancialInstrument" in report.turtle
        # ground_class was invoked for the induced classes.
        assert svc.ground_class.called
        # The report carries the per-class ConceptMatch records (→ matches.json)
        # for the review UI's candidate count + override editor. The fake only
        # returns candidates for the grounded class, so exactly one match with
        # source_table = its label lands here.
        assert len(report.groundingMatches) == 1
        m = report.groundingMatches[0]
        assert m["source_table"] == "Financial Instrument"
        assert m["matched_class_uri"] == "http://fibo/FinancialInstrument"
        assert m["candidates"]

    def test_grounding_matches_empty_when_disabled(self):
        report = UnstructuredInductionPipeline().run(_make_config())
        assert report.groundingMatches == []

    def test_grounding_failure_is_fail_open(self):
        from coa_ontology.inducer.services.grounding import GroundingService

        svc = MagicMock(spec=GroundingService)
        svc.ground_class.side_effect = RuntimeError("bedrock down")
        # The whole run must still succeed with zero grounded classes.
        report = UnstructuredInductionPipeline().run(
            _make_config(grounding_service=svc, grounding_ontology_ids=["fibo"])
        )
        assert report.foundationalGroundedCount == 0
        assert report.tboxClassCount > 0  # induction itself still produced classes


class TestBuildGroundingInputsDedup:
    """Regression: two class labels that collapse to the SAME minted class_iri
    must produce ONE grounding input, not two — otherwise the same IRI is
    grounded twice from independent service calls and can receive conflicting
    decisions (e.g. subClassOf to two different foundational parents)."""

    def test_same_class_iri_from_two_labels_yields_one_input(self):
        shared_iri = "http://ex.org/o#Instrument"
        nr1, nr2 = MagicMock(), MagicMock()
        nr1.class_iri = shared_iri
        nr2.class_iri = shared_iri  # SAME canonical IRI, different label
        results = {"Financial Instrument": nr1, "FinancialInstrument": nr2}
        descriptions = {"Financial Instrument": "d1", "FinancialInstrument": "d2"}
        index = MagicMock()
        index.embed_cache = {
            _build_embedding_text("Financial Instrument", "d1"): [0.1] * 8,
            _build_embedding_text("FinancialInstrument", "d2"): [0.2] * 8,
        }

        inputs = _build_grounding_inputs(results, descriptions, index)

        assert len(inputs) == 1
        assert inputs[0].class_iri == shared_iri

    def test_distinct_iris_each_yield_an_input(self):
        nr1, nr2 = MagicMock(), MagicMock()
        nr1.class_iri = "http://ex.org/o#A"
        nr2.class_iri = "http://ex.org/o#B"
        results = {"A": nr1, "B": nr2}
        descriptions = {"A": "da", "B": "db"}
        index = MagicMock()
        index.embed_cache = {
            _build_embedding_text("A", "da"): [0.1] * 8,
            _build_embedding_text("B", "db"): [0.2] * 8,
        }

        inputs = _build_grounding_inputs(results, descriptions, index)

        assert {i.class_iri for i in inputs} == {"http://ex.org/o#A", "http://ex.org/o#B"}

    def test_first_label_without_vector_does_not_block_later_usable_one(self):
        """If the first label for an IRI has no cached vector, a later label for
        the same IRI that DOES have one must still be grounded (not dropped)."""
        shared_iri = "http://ex.org/o#Instrument"
        nr1, nr2 = MagicMock(), MagicMock()
        nr1.class_iri = shared_iri
        nr2.class_iri = shared_iri
        results = {"NoVector": nr1, "HasVector": nr2}
        descriptions = {"NoVector": "d1", "HasVector": "d2"}
        index = MagicMock()
        # Only the SECOND label has a cached vector.
        index.embed_cache = {_build_embedding_text("HasVector", "d2"): [0.2] * 8}

        inputs = _build_grounding_inputs(results, descriptions, index)

        assert len(inputs) == 1
        assert inputs[0].label == "HasVector"
