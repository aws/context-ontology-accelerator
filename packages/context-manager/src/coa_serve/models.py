# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Request/response models matching LLD QueryResult Smithy schema."""

from __future__ import annotations

import math

from coa_common.constants import MAX_QUERY_CODEPOINTS, validate_query_text
from pydantic import BaseModel, Field, field_serializer, field_validator

from .mode import LEGACY_MODE_ALIASES

_MAX_DETAIL_LENGTH = 500  # Matches trace.py centralized truncation

# Cap on how much of a rejected value is echoed back in a validation message.
# The value is attacker-controlled and unbounded (query is capped at 4000 chars,
# but options is a free-form dict), so interpolating it whole lets a caller
# inflate the exception -- and anything derived from it -- to the size of their
# payload. 80 chars is enough to recognize which input was rejected.
_MAX_ERROR_VALUE_REPR = 80

# Bounds on a single dimension filter, in the spirit of the ``query`` (4000) and
# ``namespace`` (128) caps above: ``options`` is a free-form dict, so without
# these a caller can put megabytes into one filter. Neither can reach the SQL —
# a name must match an ASCII placeholder of a declared dimension, and anything
# unmatched now fails closed — but both are carried through the request and can
# reach traces and logs, so bound them at the boundary.
_MAX_DIMENSION_NAME_LENGTH = 128  # Matches the namespace cap
_MAX_DIMENSION_VALUE_LENGTH = 4000  # Matches the query cap


def _clip(value: object) -> str:
    """Return ``repr(value)`` truncated to a bounded length for error messages."""
    text = repr(value)
    return text if len(text) <= _MAX_ERROR_VALUE_REPR else text[:_MAX_ERROR_VALUE_REPR] + "...(truncated)"


class ConfidenceScore(BaseModel):
    """A resolution confidence score in [0, 1] with an optional rationale."""

    score: float = Field(..., ge=0.0, le=1.0)
    rationale: str = ""


class TraceStep(BaseModel):
    """One step in a query's processing trace, with timing and optional detail."""

    step: str
    status: str
    duration_ms: int = Field(0, alias="durationMs")
    detail: str | dict | None = None
    tool_used: str | None = Field(None, alias="toolUsed")
    parallel_group: str | None = Field(
        None, alias="parallelGroup", description="Groups concurrent children under a parent step"
    )
    wall_ms: int | None = Field(
        None, alias="wallMs", description="Wall-clock time for a parallel group (elapsed, not summed)"
    )
    graph_seed_mode: str | None = Field(
        None,
        alias="graphSeedMode",
        description="How the graph_traverse step got its seeds: 'uri_hop' (ontology URIs) or 'keyword' (ASCII labels)",
    )

    model_config = {"populate_by_name": True}

    @field_serializer("detail")
    def _truncate_detail(self, value: str | dict | None) -> str | dict | None:
        """Truncate detail values in client-facing serialization (CloudWatch gets full)."""
        if isinstance(value, str) and len(value) > _MAX_DETAIL_LENGTH:
            return value[:_MAX_DETAIL_LENGTH] + "..."
        if isinstance(value, dict):
            return {
                k: (v[:_MAX_DETAIL_LENGTH] + "..." if isinstance(v, str) and len(v) > _MAX_DETAIL_LENGTH else v)
                for k, v in value.items()
            }
        return value


class InvokeRequest(BaseModel):
    """Inbound query request: the question, namespace, auth profile, and options."""

    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CODEPOINTS)

    @field_validator("query", mode="before")
    @classmethod
    def _strip_query(cls, value: object) -> str:
        """Validate the raw bound before stripping or downstream segmentation."""
        return validate_query_text(value)

    namespace: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    profile: dict = Field(default_factory=dict)
    options: dict = Field(default_factory=dict)

    @field_validator("options", mode="after")
    @classmethod
    def _validate_retriever_strategy(cls, options: dict) -> dict:
        """Validate ``options.retrieverStrategy`` against :class:`RetrieverStrategy`.

        When the ``retrieverStrategy`` key is present, its value must be a valid
        ``RetrieverStrategy`` member; an invalid value raises ``ValidationError``
        (surfaced as the existing 400 path in ``main.py``).
        Absence of the key is valid, and the key stays inside ``options`` (no new
        top-level field). The import is local so this module does not eagerly pull
        in the strategy registry / graphrag import chain.
        """
        if "retrieverStrategy" in options:
            # Lazy import: validating against enum members does not import
            # graphrag_toolkit (the registry's build_engine imports are lazy).
            from .lexical.strategies import RetrieverStrategy

            value = options["retrieverStrategy"]
            try:
                RetrieverStrategy(value)
            except ValueError:
                valid = ", ".join(s.value for s in RetrieverStrategy)
                raise ValueError(f"Invalid retrieverStrategy {value!r}; must be one of: {valid}") from None
        return options

    @field_validator("options", mode="after")
    @classmethod
    def _validate_exclude_tools(cls, options: dict) -> dict:
        """Validate ``options.excludeTools`` — per-request agentic tool ablation.

        A list of agentic Tool names to WITHHOLD from the planner for this request,
        on top of the automatic composition gating. A tool the planner never sees
        cannot be selected, so this isolates one tool's contribution (e.g. run the
        loop without ``vector_search`` to measure whether it is redundant with the
        chunk-based strategies).

        Names are not validated against the live registry — the registry is built
        per deployment and an unknown name is a harmless no-op (``specs(exclude=)``
        simply matches nothing) — but the value must be a list of strings so a
        malformed request fails fast rather than silently ablating nothing.
        """
        if "excludeTools" in options:
            value = options["excludeTools"]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"Invalid excludeTools {value!r}; must be a list of tool-name strings")
        return options

    @field_validator("options", mode="after")
    @classmethod
    def _validate_mode(cls, options: dict) -> dict:
        """Validate ``options.mode`` — the per-request EXECUTION mode selector.

        Mode is orthogonal to the tier cascade (it selects HOW a request is
        executed, not which answer-shape wins), so the field is ``mode``, not
        ``tier3Mode``. Accepted values:

        * ``"deep-reasoning"`` — force the multi-step reasoning loop for this request.
        * ``"standard"`` — force the original single-shot path
          (``KnowledgeRetriever``: parallel retrieve + one synthesis).

        ``"agentic"`` is the pre-rename spelling of ``"deep-reasoning"`` and is still
        accepted (see ``mode.LEGACY_MODE_ALIASES``) so callers pinned to it do not
        start getting a 400 on upgrade. It is not advertised in the error message.

        Absence follows the deployment default (``TIER3_STRATEGY``), which ships as
        ``lexical-baseline`` — so standard is the default and deep reasoning is opt-in.
        ``"standard"`` stays an explicit opt-OUT for deployments that set
        ``TIER3_STRATEGY=deep-reasoning``.
        """
        if "mode" in options:
            value = options["mode"]
            if value not in ("deep-reasoning", "standard", *LEGACY_MODE_ALIASES):
                raise ValueError(f"Invalid mode {value!r}; must be 'deep-reasoning' or 'standard'")
        return options

    @field_validator("options", mode="after")
    @classmethod
    def _normalize_dimensions(cls, options: dict) -> dict:
        """Normalize ``options.dimensions`` from the WIRE shape to the internal one.

        The API contract (``serve.smithy``: ``dimensions: DimensionFilterList``)
        declares a LIST of ``DimensionFilter`` objects — ``[{"name", "value",
        "operator"?}, ...]`` — but ``MetricResolver.substitute_dimensions`` binds
        placeholders from a NAME->VALUE mapping. Without a conversion here, a
        contract-conformant caller reaches ``.items()`` with a list and gets
        ``AttributeError`` → a blanket 500.

        Normalizing here rather than deeper in keeps the conversion at the one
        boundary where the wire shape is known and is shared by BOTH callers (the
        Data Layer REST handler and the Playground SSE path, which construct the
        same ``InvokeRequest``). ``substitute_dimensions`` keeps its dict contract.

        A malformed payload raises ``ValueError`` → ``ValidationError`` → the
        existing 400 path in ``main.py``, so bad input is a client error rather
        than an ``AttributeError`` surfacing as "Query resolution failed".

        A dict is also accepted — it is the shape internal callers and the existing
        unit tests pass, and BOTH transports can carry one (the SSE path forwards
        ``options`` verbatim, and the REST handler forwards a top-level
        ``dimensions`` object unchanged; nothing schema-checks it upstream, as there
        is no API Gateway request validator). It therefore gets the SAME per-entry
        checks as the list shape rather than a free pass: a dict that skipped them
        could still bind ``'None'`` or a Python repr into the SQL.
        """
        if "dimensions" not in options:
            return options

        raw = options["dimensions"]
        # Copy rather than mutate in place: ``options`` is the dict the caller
        # passed in (``main.py`` hands over ``payload["options"]``), and rewriting
        # a caller's object as a validation side effect is surprising — a retry or
        # a log of the original payload would see the normalized shape.
        options = dict(options)
        if raw is None or raw == [] or raw == {}:
            # Absent-equivalent: drop the key so the Tier-1 no-dimensions path
            # (a falsy default) is taken rather than substituting nothing.
            options.pop("dimensions")
            return options

        # Reduce both accepted shapes to (name, value, operator) triples so the
        # checks below run exactly once, over both. A dict carries no operator.
        if isinstance(raw, dict):
            entries = [(k, v, None) for k, v in raw.items()]
        elif isinstance(raw, list):
            entries = []
            for item in raw:
                if not isinstance(item, dict):
                    raise ValueError(
                        f"Invalid dimension filter {_clip(item)}; must be an object with 'name' and 'value'"
                    )
                # Absent vs present-but-invalid are reported separately: "'name' is
                # required" tells the caller to add the field, whereas the
                # non-empty-string error below would say their (missing) name is
                # the wrong shape.
                for required in ("name", "value"):
                    if required not in item:
                        raise ValueError(f"Invalid dimension filter {_clip(item)}; '{required}' is required")
                entries.append((item["name"], item["value"], item.get("operator")))
        else:
            raise ValueError(f"Invalid dimensions {_clip(raw)}; must be a list of {{name, value}} objects")

        normalized: dict[str, object] = {}
        seen: set[str] = set()
        for name, value, operator in entries:
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Invalid dimension name {_clip(name)}; must be a non-empty string")
            # Trim: the placeholder lookup is an exact (lowercased) key match, so a
            # padded " region " would never match ``:region`` and the filter would
            # be silently DROPPED — Tier-1 bypassed as if unfiltered.
            name = name.strip()
            if len(name) > _MAX_DIMENSION_NAME_LENGTH:
                raise ValueError(
                    f"Invalid dimension name {_clip(name)}; must be at most {_MAX_DIMENSION_NAME_LENGTH} characters"
                )
            # ``value`` is @required String in the contract, and substitute_dimensions
            # renders whatever it gets via str() — so a null would bind the literal
            # 'None' and an object/array would leak a Python repr into the SQL, both
            # as a WRONG answer with a 200. Accept only scalars; bools/ints are kept
            # as-is because substitute_dimensions has typed-literal handling for them
            # (TRUE/FALSE, unquoted numbers).
            #
            # bool before int/float is deliberate: bool IS an int in Python, and
            # isfinite() must not run on it. NaN/Infinity are excluded because
            # json.loads accepts those non-standard tokens and sqlglot renders them
            # as the BARE words nan/inf, which SQL engines parse as a COLUMN
            # reference rather than a literal — silently comparing two columns.
            if isinstance(value, bool):
                pass
            elif isinstance(value, int):
                pass  # always finite; isfinite() would raise OverflowError past float range
            elif isinstance(value, float):
                if not math.isfinite(value):
                    raise ValueError(
                        f"Invalid dimension value {_clip(value)} for {_clip(name)}; must be a finite number"
                    )
            elif not isinstance(value, str):
                raise ValueError(
                    f"Invalid dimension value {_clip(value)} for {_clip(name)}; must be a string, number, or boolean"
                )
            elif len(value) > _MAX_DIMENSION_VALUE_LENGTH:
                raise ValueError(
                    f"Invalid dimension value for {_clip(name)}; "
                    f"must be at most {_MAX_DIMENSION_VALUE_LENGTH} characters"
                )
            # Only equality is expressible in the SQL templates
            # (``substitute_dimensions`` binds a bare literal), so a non-``=``
            # operator must be REJECTED, not silently applied as equality — a
            # caller asking for ``>`` would otherwise get a wrong answer with a
            # 200. The contract now says the same thing (``serve.smithy``:
            # ``operator: DimensionOperator``, a single-member enum), but nothing
            # upstream enforces it — there is no API Gateway request validator, so
            # this check is what actually rejects. Widening the substitution to real
            # comparisons is a follow-up that would add enum members.
            #
            # Blank/whitespace counts as ABSENT (operator is optional, defaulting to
            # equality, and a generated client may serialize an unset optional
            # string as ""), so it must not 400 — deliberately more lenient than the
            # generated schema's ``enum: ["="]``.
            if operator is not None and (not isinstance(operator, str) or operator.strip() not in ("", "=")):
                raise ValueError(f"Unsupported dimension operator {_clip(operator)}; only '=' is supported")
            # A repeated name would collapse to the last value — a WRONG answer
            # returned with a 200. Reject instead: the mapping cannot express two
            # values for one dimension.
            #
            # casefold(), not the lower() used by ``substitute_dimensions``: this is
            # duplicate REJECTION, where over-matching is the safe direction. It can
            # only 400 an exotic near-duplicate ("ß"/"ss"), never admit a pair that
            # collapses during binding. (Over ASCII — all a placeholder can be — the
            # two are identical; see the note at ``substitute_dimensions``.)
            if name.casefold() in seen:
                raise ValueError(f"Duplicate dimension filter for {_clip(name)}; specify each dimension at most once")
            seen.add(name.casefold())
            normalized[name] = value

        options["dimensions"] = normalized
        return options


class QueryResult(BaseModel):
    """Resolution result: answer tier, confidence, payloads, and processing trace."""

    tier: float
    confidence: ConfidenceScore
    result_rows: list[dict] | None = Field(None, alias="resultRows")
    synthesized_answer: str | None = Field(None, alias="synthesizedAnswer")
    query_used: str | None = Field(None, alias="queryUsed")
    sparql_generated: str | None = Field(None, alias="sparqlGenerated")
    supporting_content: list[dict] | None = Field(None, alias="supportingContent")
    graph_context: dict | None = Field(None, alias="graphContext")
    trace: list[TraceStep]
    ontology_version: str | None = Field(None, alias="ontologyVersion")
    data_sources: list[str] | None = Field(None, alias="dataSources")
    guardrail_blocked: bool = Field(False, alias="guardrailBlocked")
    # OUT OF SCOPE (flag, do not implement here): `partial` stays unpopulated.
    # The partial/504-timeout flag is owned by the Data Layer and is set
    # on the timeout path (see main.py); this model does not touch it.
    partial: bool = False
    metadata: dict | None = None
    # OUT OF SCOPE (flag, do not implement here): no `conformanceWarnings`
    # field. SHACL conformance warnings are P1 and are intentionally absent
    # from this model; do NOT add a conformanceWarnings field here.
    model_config = {"populate_by_name": True}


class InvokeResponse(BaseModel):
    """Outbound API envelope wrapping a QueryResult with request/session ids."""

    result: QueryResult
    requestId: str | None = None
    sessionId: str | None = None
