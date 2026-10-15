# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Class description generator for T-Box ``rdfs:comment`` annotations.

Given a mapping of classification string → list of sampled entity surface-form
labels, this module synthesizes a concise 1-3 sentence natural-language
description of each class via an LLM. The descriptions are intended to be
attached as ``rdfs:comment`` annotations on the corresponding ``owl:Class``
declarations, replacing the primitive ``"Source entity count: N"`` format.

The LLM client is typed structurally via the ``LLMDescriptionClient``
``Protocol`` so the function can be tested with a simple mock. The existing
``LLMClient`` from ``coa_ontology.inducer.services.llm`` satisfies
this protocol structurally — its ``generate(prompt, system, max_tokens) -> str``
signature matches by definition. This module deliberately does NOT import the
concrete ``LLMClient`` class to avoid coupling the service to the Bedrock
implementation.

Each class is processed independently: a per-class LLM failure (any exception
raised by ``generate``) is caught, logged at WARNING level, and replaced with
a deterministic fallback description of the form::

    "Class representing {N} entities including: {top-5-alphabetical-labels}"

where the top-5 labels are the lexicographically smallest five (case-insensitive
ordering via ``str.casefold``), joined by ``", "``. If fewer than five labels
are available, all of them are listed; if zero, the trailing list is empty.

Classes whose label list is empty bypass the LLM entirely and use the fallback
directly to avoid wasting a request on a hallucinated description.

Usage::

    from coa_ontology.inducer.unstructured.services.description_generator import (
        generate_descriptions,
    )

    class_entities = {
        "Financial Instrument": ["Treasury Bond", "Corporate Note", "Equity Swap"],
        "Company": ["Acme Corp", "Globex", "Initech"],
    }
    descriptions = generate_descriptions(class_entities, llm_client)
    # descriptions: {"Financial Instrument": "Financial instruments ...", ...}
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from coa_common.language_tags import (
    DEFAULT_LANGUAGE,
    LanguageTagError,
    describe_language,
    needs_language_instruction,
)

from coa_ontology.inducer.services.generated_text import join_list, join_parts, render

log = logging.getLogger(__name__)

# Maximum number of entity labels included in the LLM prompt per class.
# Beyond this, prompt size grows without meaningfully improving the description.
# The first ``MAX_PROMPT_LABELS`` labels are taken in the order received (which
# is already a stable sampler-ordered list from upstream).
MAX_PROMPT_LABELS = 30

# Number of labels included in the deterministic fallback description.
FALLBACK_TOP_N = 5

_SYSTEM_PROMPT = (
    "You are an ontology engineer writing concise rdfs:comment annotations for "
    "OWL classes. Given a class label and example entity labels that belong to "
    "the class, write a factual 1-3 sentence description of what the class "
    "represents. Output plain prose only — no preamble, no markdown, no bullet "
    "lists, no quotation marks around the response. Describe only what the "
    "given examples suggest; do not invent unrelated facts."
)


def _system_prompt(base: str, namespace_language: str) -> str:
    """Return a trusted system prompt for the namespace language.

    An English namespace (primary subtag ``en``) gets ``base`` unchanged. Any
    other language appends a fixed output-language instruction. The instruction
    is application control text, so it sits in the system prompt; class and
    entity labels stay in the user prompt. Only a canonical tag and a language
    name from a fixed table are interpolated.

    An unsupported tag also gets ``base`` unchanged, as the fixed-text
    templates fall back to English. A route never passes one: it canonicalizes
    the namespace language when it resolves it.
    """
    try:
        instruct = needs_language_instruction(namespace_language)
    except LanguageTagError:
        instruct = False
    if not instruct:
        return base
    target = describe_language(namespace_language)
    return (
        base + "\n\nOutput language:\n"
        f"- Write the description in {target}, the language of this knowledge graph.\n"
        "- Keep class labels, entity labels, predicate names, model numbers, product names, units and "
        "codes exactly as given. Do not translate or transliterate them.\n"
        "- These instructions are in English for your benefit; do not treat them as evidence about "
        "the language of the output."
    )


@runtime_checkable
class LLMDescriptionClient(Protocol):
    """Structural type for the LLM client used to generate descriptions.

    Any object exposing a ``generate(prompt, system, max_tokens) -> str``
    method satisfies this protocol. The existing
    ``coa_ontology.inducer.services.llm.LLMClient`` matches by
    definition.

    Decorated with :func:`runtime_checkable` so :class:`UnstructuredInductionConfig`
    (which uses Pydantic ``arbitrary_types_allowed=True``) can validate
    field assignments via ``isinstance()``.
    """

    def generate(self, prompt: str, system: str = "", max_tokens: int = 4096) -> str:
        """Generate text for the given prompt and return it."""
        ...


def _build_user_prompt(class_label: str, labels: list[str]) -> str:
    """Build the user prompt for a single class.

    Caps the number of labels included at ``MAX_PROMPT_LABELS`` to keep prompts
    bounded. Labels are taken in the order received (stable sampler ordering).

    Args:
        class_label: The classification string (e.g. ``"Financial Instrument"``).
        labels: Entity surface-form labels for the class.

    Returns:
        A user-prompt string suitable for ``LLMDescriptionClient.generate``.
    """
    capped = labels[:MAX_PROMPT_LABELS]
    bullet_lines = "\n".join(f"- {label}" for label in capped)
    return (
        f'Class label: "{class_label}"\n'
        f"Example entity labels ({len(capped)}):\n"
        f"{bullet_lines}\n\n"
        f"Write the rdfs:comment description for this class."
    )


def _fallback_description(labels: list[str], namespace_language: str = DEFAULT_LANGUAGE) -> str:
    """Build the deterministic fallback description.

    The fallback lists the lexicographically smallest ``FALLBACK_TOP_N`` labels
    using a case-insensitive sort key (``str.casefold``) for stable ordering
    across cases like ``"acme"`` vs ``"Beta"``. If fewer than ``FALLBACK_TOP_N``
    labels are available, all of them are included; if zero, the trailing list
    is the empty string.

    Args:
        labels: Entity surface-form labels for the class.
        namespace_language: Namespace language; selects the reviewed template.

    Returns:
        A string of the form
        ``"Class representing {N} entities including: {top-5-alphabetical-labels}"``
        (or its reviewed Japanese version).
    """
    n = len(labels)
    sorted_labels = sorted(labels, key=str.casefold)
    top = sorted_labels[:FALLBACK_TOP_N]
    return render("class_fallback_entities", namespace_language, count=n, labels=join_list(top, namespace_language))


def _post_process_response(text: str) -> str:
    """Clean up an LLM response.

    Strips leading/trailing whitespace and a single pair of surrounding double
    quotes if present (a common LLM artifact when the model echoes the
    description as a quoted string).

    Args:
        text: Raw text returned by the LLM.

    Returns:
        The cleaned response, or the empty string if nothing remained after
        stripping.
    """
    cleaned = text.strip()
    if len(cleaned) >= 2 and cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1].strip()
    return cleaned


def generate_descriptions(
    class_entities: dict[str, list[str]],
    llm_client: LLMDescriptionClient,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> dict[str, str]:
    """Generate ``rdfs:comment`` descriptions for each class via an LLM.

    For each ``(class_label, labels)`` pair:

    - If ``labels`` is empty, the fallback description is used (no LLM call).
    - Otherwise, the LLM is invoked with a system prompt instructing concise
      ontology-class prose and a user prompt containing the class label and
      up to ``MAX_PROMPT_LABELS`` entity labels (in the order received).
    - On any exception from ``llm_client.generate``, or on an empty/whitespace
      response, the fallback description is used instead. Each class is
      processed independently — a failure on one class never aborts the batch.

    Args:
        class_entities: Mapping from classification string (e.g.
            ``"Financial Instrument"``) to entity surface-form labels (e.g.
            ``["Treasury Bond", "Corporate Note", ...]``). NOT entity IDs.
        llm_client: Any object satisfying the ``LLMDescriptionClient`` Protocol
            — i.e. exposing ``generate(prompt, system, max_tokens) -> str``.
        namespace_language: Namespace language. A non-English namespace adds an
            output-language instruction and uses the reviewed fallback text.

    Returns:
        Mapping from each input classification string to a non-empty
        description string. The keys exactly match the input keys (including
        for empty-label classes, where the fallback is used).
    """
    total_labels = sum(len(v) for v in class_entities.values())
    log.info(
        "Generating class descriptions: %d classes, %d total labels",
        len(class_entities),
        total_labels,
    )

    descriptions: dict[str, str] = {}

    for class_label, labels in class_entities.items():
        log.debug(
            "Class '%s': %d labels available, prompt sample size %d",
            class_label,
            len(labels),
            min(len(labels), MAX_PROMPT_LABELS),
        )

        if not labels:
            log.warning(
                "Class '%s' has no entity labels — using fallback description",
                class_label,
            )
            descriptions[class_label] = _fallback_description(labels, namespace_language)
            continue

        prompt = _build_user_prompt(class_label, labels)
        try:
            raw = llm_client.generate(prompt, system=_system_prompt(_SYSTEM_PROMPT, namespace_language))
        except Exception as exc:  # noqa: BLE001 — broad on purpose; see docstring.
            log.warning(
                "LLM call failed for class '%s' (%s: %s) — using fallback",
                class_label,
                type(exc).__name__,
                exc,
            )
            descriptions[class_label] = _fallback_description(labels, namespace_language)
            continue

        cleaned = _post_process_response(raw)
        if not cleaned:
            log.warning(
                "LLM returned empty response for class '%s' — using fallback",
                class_label,
            )
            descriptions[class_label] = _fallback_description(labels, namespace_language)
            continue

        descriptions[class_label] = cleaned

    return descriptions


# ── Enriched-context mode ───────────────────────────────────────────────
#
# The label-only ``generate_descriptions`` function above gives the LLM
# essentially just the entity surface forms — which means the LLM tends to
# fall back on its parametric memory ("a Company is a business
# organization...") rather than describing what the SPECIFIC graph
# instance actually contains. The enriched-context mode pairs each entity
# label with one or more short context strings (typically derived from
# ``__Fact__`` nodes) so the LLM has direct evidence of how the entities
# behave in this graph.
#
# The enriched API is a separate function (not a flag on
# ``generate_descriptions``) because the input shape is fundamentally
# different — a richer dict-of-list-of-records vs. a simple
# dict-of-list-of-strings. The pipeline orchestrator can choose which
# function to call based on its configuration; both share the same fallback,
# post-processing, and per-class fault-isolation semantics.

from dataclasses import dataclass, field  # noqa: E402 — grouped here next to enriched API for readability.

# Maximum number of entity contexts (label + fact bundle) per class in the
# LLM prompt. Bounded to keep prompts manageable — matches MAX_PROMPT_LABELS
# in spirit but applied to entities rather than raw labels.
MAX_PROMPT_ENTITIES = 12

# Maximum number of fact strings included per entity in the enriched prompt.
# Some entities have thousands of facts in real graphs; capping per-entity
# keeps overall prompt size predictable.
MAX_FACTS_PER_ENTITY = 8

# Total cap on facts in a single enriched prompt across all entities for one
# class. Defends against the pathological case of MAX_PROMPT_ENTITIES *
# MAX_FACTS_PER_ENTITY exceeding what the model can usefully read.
MAX_FACTS_PER_PROMPT = 60


_ENRICHED_SYSTEM_PROMPT = (
    "You are an ontology engineer writing concise rdfs:comment annotations "
    "for OWL classes. Given a class label, example entity labels that belong "
    "to the class, and short factual statements observed about each entity in "
    "a knowledge graph, write a factual 1-3 sentence description of what the "
    "class represents IN THIS GRAPH. Ground your description in the supplied "
    "facts; prefer specifics from the facts over generic textbook definitions. "
    "Output plain prose only — no preamble, no markdown, no bullet lists, no "
    "quotation marks around the response. Do not invent facts that aren't "
    "supported by the supplied evidence."
)


@dataclass
class EntityContext:
    """One entity with its associated context for the enriched prompt.

    Attributes:
        label: The entity surface-form label (e.g. ``"foundry partners"``).
        facts: Short context strings, typically derived from ``__Fact__``
            nodes — e.g. ``["foundry partners TYPE OF suppliers",
            "foundry partners SERVES customers"]``. May be empty: the entity
            is then prompted as label-only.
    """

    label: str
    facts: list[str] = field(default_factory=list)


def _build_enriched_user_prompt(
    class_label: str,
    contexts: list[EntityContext],
) -> str:
    """Build the user prompt for a single class with per-entity facts.

    Caps the number of entities at ``MAX_PROMPT_ENTITIES`` and the number of
    facts per entity at ``MAX_FACTS_PER_ENTITY``. A global cap of
    ``MAX_FACTS_PER_PROMPT`` is enforced across all entities (oldest entries
    in the input order kept first).

    Args:
        class_label: The classification string.
        contexts: ``EntityContext`` records in stable order. The first
            ``MAX_PROMPT_ENTITIES`` are taken.

    Returns:
        A user-prompt string suitable for ``LLMDescriptionClient.generate``.
    """
    capped_entities = contexts[:MAX_PROMPT_ENTITIES]

    lines: list[str] = [f'Class label: "{class_label}"']
    lines.append(f"Entities and their facts ({len(capped_entities)}):")
    facts_remaining = MAX_FACTS_PER_PROMPT
    for ec in capped_entities:
        lines.append(f"- {ec.label}")
        # Take up to MAX_FACTS_PER_ENTITY facts, but never exceed the global
        # remaining budget.
        budget = min(MAX_FACTS_PER_ENTITY, facts_remaining)
        if budget <= 0 or not ec.facts:
            continue
        for fact_str in ec.facts[:budget]:
            lines.append(f"    · {fact_str}")
            facts_remaining -= 1
    lines.append("")
    lines.append("Write the rdfs:comment description for this class.")
    return "\n".join(lines)


def _entity_contexts_to_label_list(
    contexts: list[EntityContext],
) -> list[str]:
    """Helper: flatten enriched contexts back to the label-only fallback input.

    Used when the LLM call fails for a class so the deterministic
    ``_fallback_description`` can be reused without duplication.
    """
    return [ec.label for ec in contexts]


def generate_descriptions_with_context(
    class_contexts: dict[str, list[EntityContext]],
    llm_client: LLMDescriptionClient,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> dict[str, str]:
    """Enriched variant of :func:`generate_descriptions`.

    Same contract as ``generate_descriptions`` but takes ``EntityContext``
    records instead of bare label strings. Each context bundles an entity's
    label with a list of short factual statements observed about it in the
    source graph (typically formatted ``__Fact__`` nodes). The LLM prompt
    interleaves entities with their facts so the model has direct evidence
    rather than relying on parametric memory.

    Behaviour mirrors ``generate_descriptions``:

    - If ``contexts`` is empty for a class, fall back without an LLM call.
    - On any exception from ``llm_client.generate`` or empty/whitespace
      response, fall back. The fallback uses only the entity LABELS (not
      the facts) since its purpose is to be deterministic and brief.
    - Each class is processed independently.

    Args:
        class_contexts: Mapping from classification string to a list of
            :class:`EntityContext` records.
        llm_client: Any object satisfying the ``LLMDescriptionClient`` Protocol.
        namespace_language: Namespace language, as for :func:`generate_descriptions`.

    Returns:
        Mapping from each input classification string to a non-empty
        description string. Keys exactly match input keys.
    """
    total_entities = sum(len(v) for v in class_contexts.values())
    total_facts = sum(len(ec.facts) for v in class_contexts.values() for ec in v)
    log.info(
        "Generating enriched class descriptions: %d classes, %d entities, %d total facts",
        len(class_contexts),
        total_entities,
        total_facts,
    )

    descriptions: dict[str, str] = {}

    for class_label, contexts in class_contexts.items():
        labels_for_fallback = _entity_contexts_to_label_list(contexts)
        log.debug(
            "Class '%s': %d entities, avg %.1f facts/entity",
            class_label,
            len(contexts),
            (sum(len(ec.facts) for ec in contexts) / len(contexts) if contexts else 0.0),
        )

        if not contexts:
            log.warning(
                "Class '%s' has no entity contexts — using fallback description",
                class_label,
            )
            descriptions[class_label] = _fallback_description(labels_for_fallback, namespace_language)
            continue

        prompt = _build_enriched_user_prompt(class_label, contexts)
        try:
            raw = llm_client.generate(prompt, system=_system_prompt(_ENRICHED_SYSTEM_PROMPT, namespace_language))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Enriched LLM call failed for class '%s' (%s: %s) — using fallback",
                class_label,
                type(exc).__name__,
                exc,
            )
            descriptions[class_label] = _fallback_description(labels_for_fallback, namespace_language)
            continue

        cleaned = _post_process_response(raw)
        if not cleaned:
            log.warning(
                "Enriched LLM returned empty response for class '%s' — using fallback",
                class_label,
            )
            descriptions[class_label] = _fallback_description(labels_for_fallback, namespace_language)
            continue

        descriptions[class_label] = cleaned

    return descriptions


# ── Schema-context mode ─────────────────────────────────────────────────
#
# The ``instance``-style enriched mode (above) feeds the LLM raw per-entity
# facts. That makes descriptions vivid but ALSO makes them too instance-
# specific — e.g. an LLM seeing facts like "company NET INCOME APRIL 30 2023
# $2,043" tends to reproduce those values in the description, which is wrong
# for an rdfs:comment that should describe the CLASS, not particular members.
#
# The ``schema``-context mode below gives the LLM a TYPE-level summary
# instead: which predicates connect this class to which other classes, with
# frequencies. This is exactly the T-Box signal an OWL class description
# should be informed by.
#
# Note: this mode currently uses ONLY inter-class predicate aggregates
# (the __SYS_RELATION__ layer). Extracting "attribute-style" predicate types
# (i.e. owl:DatatypeProperty candidates) from per-entity predicate-only
# facts requires more sophisticated parsing — see the optional task in
# tasks.md ("Edge-type predicate summarization and embedding").


_SCHEMA_SYSTEM_PROMPT = (
    "You are an ontology engineer writing concise rdfs:comment annotations "
    "for OWL classes. Given a class label and a TYPE-LEVEL summary of how "
    "this class connects to other classes in a knowledge graph (predicate "
    "labels and target classes, with observed frequencies), write a factual "
    "1-3 sentence description of what the class represents and how it "
    "behaves in this graph. Describe the GENERAL ROLE of the class — the "
    "kinds of relationships its members participate in, the kinds of "
    "classes they connect to — NOT specific instance values, dates, or "
    "names. Output plain prose only — no preamble, no markdown, no bullet "
    "lists, no quotation marks around the response."
)


@dataclass
class SchemaEdge:
    """One predicate-target (or source-predicate) edge type at the class level.

    Attributes:
        predicate: The (normalized) predicate label.
        other_class: The class on the other end of the edge — the target
            class for outgoing edges, the source class for incoming.
        frequency: Total observation count from the ``__SYS_RELATION__``
            aggregates.
    """

    predicate: str
    other_class: str
    frequency: int


@dataclass
class ClassSchemaContext:
    """T-Box schema fingerprint for one class.

    Attributes:
        class_label: The class whose schema this describes.
        outgoing: Edges where ``class_label`` is the source. Sorted by
            frequency descending, ties broken lexicographically.
        incoming: Edges where ``class_label`` is the target. Same sort order.
    """

    class_label: str
    outgoing: list[SchemaEdge] = field(default_factory=list)
    incoming: list[SchemaEdge] = field(default_factory=list)


# Cap on how many outgoing/incoming edges to include in the prompt per
# class. Beyond this the LLM gets noise rather than signal.
MAX_SCHEMA_EDGES_PER_DIRECTION = 12


def _build_schema_user_prompt(ctx: ClassSchemaContext) -> str:
    """Build the user prompt for one class's schema fingerprint."""
    out_capped = ctx.outgoing[:MAX_SCHEMA_EDGES_PER_DIRECTION]
    in_capped = ctx.incoming[:MAX_SCHEMA_EDGES_PER_DIRECTION]

    lines: list[str] = [f'Class label: "{ctx.class_label}"']

    if out_capped:
        lines.append("")
        lines.append(f"Outgoing relationships ({ctx.class_label} → other class), top {len(out_capped)} by frequency:")
        for edge in out_capped:
            lines.append(
                f"  - {ctx.class_label} --{edge.predicate}--> {edge.other_class}  (frequency {edge.frequency})"
            )
    else:
        lines.append("")
        lines.append("Outgoing relationships: (none observed)")

    if in_capped:
        lines.append("")
        lines.append(f"Incoming relationships (other class → {ctx.class_label}), top {len(in_capped)} by frequency:")
        for edge in in_capped:
            lines.append(
                f"  - {edge.other_class} --{edge.predicate}--> {ctx.class_label}  (frequency {edge.frequency})"
            )
    else:
        lines.append("")
        lines.append("Incoming relationships: (none observed)")

    lines.append("")
    lines.append(
        "Write the rdfs:comment description for this class. Focus on the "
        "general role and types of relationships the class participates in, "
        "not on specific instance values."
    )
    return "\n".join(lines)


def _schema_fallback_description(ctx: ClassSchemaContext, namespace_language: str = DEFAULT_LANGUAGE) -> str:
    """Deterministic fallback for the schema mode.

    Falls back to a brief textual summary of the top edges, e.g.:

        "Class representing entries connected via OPERATES_IN, RELIES_ON,
        OWNS to other classes in the graph."

    If neither outgoing nor incoming edges are present, falls back to a
    minimal "Class with no observed inter-class relationships." sentence.
    """
    lang = namespace_language
    top_out = ctx.outgoing[:FALLBACK_TOP_N]
    top_in = ctx.incoming[:FALLBACK_TOP_N]
    if not top_out and not top_in:
        return render("class_fallback_no_relationships", lang, label=ctx.class_label)
    parts: list[str] = []
    if top_out:
        preds = join_list(sorted({e.predicate for e in top_out}), lang)
        parts.append(render("class_fallback_outgoing", lang, predicates=preds))
    if top_in:
        preds = join_list(sorted({e.predicate for e in top_in}), lang)
        parts.append(render("class_fallback_incoming", lang, predicates=preds))
    return render("class_fallback_edges", lang, label=ctx.class_label, edges=join_parts(parts, lang))


def generate_descriptions_with_schema(
    class_schemas: dict[str, ClassSchemaContext],
    llm_client: LLMDescriptionClient,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> dict[str, str]:
    """Schema-context variant of :func:`generate_descriptions`.

    Generates rdfs:comment descriptions from a TYPE-LEVEL summary of each
    class's connectivity (predicate labels and target classes with
    frequencies), rather than from per-entity instance facts. Useful when
    the goal is a description of what the CLASS represents in the
    ontology, not what specific members happen to look like.

    Behaviour mirrors :func:`generate_descriptions`:

    - Each class is processed independently.
    - On any LLM exception, empty/whitespace response, fall back to
      :func:`_schema_fallback_description`.
    - Empty schema (no outgoing AND no incoming edges) bypasses the LLM
      and uses the fallback directly.

    Args:
        class_schemas: Mapping from class label to its
            :class:`ClassSchemaContext`.
        llm_client: Any object satisfying :class:`LLMDescriptionClient`.
        namespace_language: Namespace language, as for :func:`generate_descriptions`.

    Returns:
        Mapping from each input class label to a non-empty description.
    """
    log.info(
        "Generating schema-context class descriptions: %d classes (total outgoing edges in input: %d, incoming: %d)",
        len(class_schemas),
        sum(len(s.outgoing) for s in class_schemas.values()),
        sum(len(s.incoming) for s in class_schemas.values()),
    )

    descriptions: dict[str, str] = {}

    for class_label, ctx in class_schemas.items():
        log.debug(
            "Class %r: %d outgoing edges, %d incoming edges",
            class_label,
            len(ctx.outgoing),
            len(ctx.incoming),
        )

        if not ctx.outgoing and not ctx.incoming:
            log.warning(
                "Class %r has no schema edges — using fallback description",
                class_label,
            )
            descriptions[class_label] = _schema_fallback_description(ctx, namespace_language)
            continue

        prompt = _build_schema_user_prompt(ctx)
        try:
            raw = llm_client.generate(prompt, system=_system_prompt(_SCHEMA_SYSTEM_PROMPT, namespace_language))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Schema LLM call failed for %r (%s: %s) — using fallback",
                class_label,
                type(exc).__name__,
                exc,
            )
            descriptions[class_label] = _schema_fallback_description(ctx, namespace_language)
            continue

        cleaned = _post_process_response(raw)
        if not cleaned:
            log.warning(
                "Schema LLM returned empty response for %r — using fallback",
                class_label,
            )
            descriptions[class_label] = _schema_fallback_description(ctx, namespace_language)
            continue

        descriptions[class_label] = cleaned

    return descriptions
