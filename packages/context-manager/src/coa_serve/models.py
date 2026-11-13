# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Request/response models matching the serve.smithy QueryResult shape."""

from __future__ import annotations

import math

from coa_common.constants import MAX_QUERY_CODEPOINTS, validate_query_text
from pydantic import BaseModel, Field, PrivateAttr, field_serializer, field_validator

from .llm_usage import LlmUsageSnapshot
from .mode import LEGACY_MODE_ALIASES
from .request_caps import SQL_WRITER_EFFORT_LEVELS

_MAX_DETAIL_LENGTH = 500

# Cap on how much of a rejected value is echoed back in a validation message.
# The value is attacker-controlled and unbounded (query is capped at 4000 chars,
# but options is a free-form dict), so interpolating it whole lets a caller
# inflate the exception -- and anything derived from it -- to the size of their
# payload. 80 chars is enough to recognize which input was rejected.
_MAX_ERROR_VALUE_REPR = 80

# Bounds on a single dimension filter, in the spirit of the ``query`` (4000) and
# ``namespace`` (128) caps above: ``options`` is a free-form dict, so without
# these a caller can put megabytes into one filter. Neither can reach the SQL,
# but both are carried through the request into traces and logs.
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

        When the key is present its value must be a valid ``RetrieverStrategy``
        member; an invalid value raises ``ValidationError`` (a 400). Absence is valid.
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
    def _validate_deep_reasoning_levers(cls, options: dict) -> dict:
        """Validate the per-request deep-reasoning options (all optional).

        An option the request leaves unset gets its recommended value on the
        deep-reasoning path (``tier3.agentic.defaults``); validation sees only what
        the request sent.

        * ``ontologyLookupMode`` (``"planner"`` | ``"preload"``) — ``preload`` resolves
          the ontology once at session start, outside the step budget, instead of
          offering ``ontology_lookup`` to the planner.
        * ``readDocument`` (bool) — offer the planner the ``read_document`` tool;
          ``readDocumentMaxChars`` (int) sets its default window size.
        * ``readDocumentAnchor`` (bool) — with ``readDocument``, centre each window
          on the relevant passage instead of paging from offset 0.
        * ``entityRouting`` (bool) — offer ``find_documents`` (names in the question
          -> the source documents mentioning them) and route the question's named
          terms once before the loop.
        * ``prefetchMode`` (``"assess"`` | ``"off"``) — before the loop, run the SQL writer on
          the whole question and read the top documents in parallel, then check
          sufficiency once and skip the loop when the evidence is enough.
        * ``plannerParseRetry`` (bool) — one repair turn on an unparseable planner
          reply.
        * ``tokenCaps`` ({planner|synthesizer|nl2sql: int}) — per-request output token caps.
        * ``loopFixes`` (bool) — drop unknown planner fields instead of rejecting
          the call, do not call the planner or start a tool once no time is left
          for the tool, give back the step of a read that returned nothing new, and
          drop privacy-mask placeholders from the routed names.
        * ``sufficiencyGuards`` (bool) — hold back a positive sufficiency check (at most
          twice per loop) while a top name-matched document has been read only in
          part. Takes effect with ``entityRouting``.
        * ``fastPathSpeculation`` (bool) — with ``prefetchMode: "assess"``, write the
          answer in parallel with the "enough?" check and keep it only if the check
          passes; skip the check when a sufficiency guard already holds it back.
          Blocking requests only.
        * ``keepSlowSql`` (bool) — with ``prefetchMode: "assess"``, a prefetch SQL call
          that outlives its timeout keeps running; the loop uses its result instead
          of writing a new query, and it is cancelled before the answer is written.
        * ``singleCallSteps`` (bool) — the planner's next-step call also decides when
          to stop (an empty proposal ends the loop), so each step is one model call.
        * ``sqlWithEvidence`` (bool) / ``sqlEvidenceMaxChars`` (int) — nl_to_sql
          hands the loop's gathered document evidence to the SQL writer.
        * ``sqlOriginalQuestion`` / ``sqlTimeRules`` / ``sqlComputeMetric`` (bool) —
          SQL-writer adjustments (original question for constraints, direct
          timestamp comparisons, requested metric computed in SQL). ``sqlComputeMetric``
          applies inside Deep Reasoning only.
        * ``sqlInAnswer`` (bool) — the answer step sees the SQL behind the structured
          rows and treats the rows as evidence for what those queries compute.
        * ``sqlRevision`` (bool) — nl_to_sql also accepts a ``revise`` reason; the
          writer then revises the request's previous query from its SQL and rows.
        * ``sqlResultCheck`` (bool) — with ``prefetchMode: "assess"`` and
          ``sqlRevision``, before the prefetch SQL result is accepted as enough, one
          model call checks it for a defect visible in the rows; a named defect
          rewrites the query once and the new rows replace the old.
        * ``sqlWriterEffort`` (``"low"`` / ``"medium"`` / ``"high"``) — the SQL
          writer's model effort level (how much it reasons before writing) on its
          generate and correct calls; unset keeps the model's default on a direct
          Tier-2 request (Deep Reasoning fills in the recommended ``"medium"``).
        * ``tokenCaps.nl2sql``, ``sqlTimeRules`` and ``sqlWriterEffort`` also apply
          to a direct Tier-2 NL→SQL request.

        Only types and enumerations are checked here; ranges are clamped where the
        option is used. A malformed value fails fast instead of silently running
        the default.
        """
        for key in ("sqlFeedback", "originalQuestion", "evidenceMaxChars", "computeMetric"):
            # Set internally by the deep-reasoning SQL tool (sqlRevision /
            # sqlOriginalQuestion / sqlWithEvidence / sqlComputeMetric); a caller
            # must not shape the SQL writer's prompt this way.
            if key in options:
                raise ValueError(f"{key} is set internally and cannot be sent in a request")
        if "tokenCaps" in options:
            caps = options["tokenCaps"]
            if not isinstance(caps, dict) or any(
                key not in ("planner", "synthesizer", "nl2sql") or isinstance(v, bool) or not isinstance(v, int)
                for key, v in caps.items()
            ):
                raise ValueError(f"Invalid tokenCaps {caps!r}; must map planner/synthesizer/nl2sql to integers")
        for key in (
            "readDocument",
            "readDocumentAnchor",
            "entityRouting",
            "sqlWithEvidence",
            "sqlOriginalQuestion",
            "sqlTimeRules",
            "sqlComputeMetric",
            "plannerParseRetry",
            "loopFixes",
            "sufficiencyGuards",
            "singleCallSteps",
            "fastPathSpeculation",
            "keepSlowSql",
            "sqlRevision",
            "sqlInAnswer",
            "sqlResultCheck",
        ):
            if key in options and not isinstance(options[key], bool):
                raise ValueError(f"Invalid {key} {options[key]!r}; must be a boolean")
        for key in ("readDocumentMaxChars", "sqlEvidenceMaxChars"):
            value = options.get(key)
            if key in options and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"Invalid {key} {value!r}; must be an integer")
        if "sqlWriterEffort" in options and options["sqlWriterEffort"] not in SQL_WRITER_EFFORT_LEVELS:
            raise ValueError(
                f"Invalid sqlWriterEffort {options['sqlWriterEffort']!r}; must be 'low', 'medium' or 'high'"
            )
        if "prefetchMode" in options and options["prefetchMode"] not in ("assess", "off"):
            raise ValueError(f"Invalid prefetchMode {options['prefetchMode']!r}; must be 'assess' or 'off'")
        if "ontologyLookupMode" in options and options["ontologyLookupMode"] not in ("planner", "preload"):
            raise ValueError(
                f"Invalid ontologyLookupMode {options['ontologyLookupMode']!r}; must be 'planner' or 'preload'"
            )
        return options

    @field_validator("options", mode="after")
    @classmethod
    def _validate_mode(cls, options: dict) -> dict:
        """Validate ``options.mode`` — the per-request EXECUTION mode selector.

        Accepted values:

        * ``"deep-reasoning"`` — force the multi-step reasoning loop for this request.
        * ``"standard"`` — force the single-shot path
          (``KnowledgeRetriever``: parallel retrieve + one synthesis).

        The legacy ``"agentic"`` spelling is accepted too (see
        ``mode.LEGACY_MODE_ALIASES``) but not advertised in the error message.
        Absence follows the deployment default (``TIER3_STRATEGY``).
        """
        if "mode" in options:
            value = options["mode"]
            if value not in ("deep-reasoning", "standard", *LEGACY_MODE_ALIASES):
                raise ValueError(f"Invalid mode {value!r}; must be 'deep-reasoning' or 'standard'")
        return options

    @field_validator("options", mode="after")
    @classmethod
    def _validate_include_llm_usage(cls, options: dict) -> dict:
        """Validate ``options.includeLlmUsage`` — opt in to ``QueryResult.llmUsage``.

        Observability only: it never changes routing or execution. Must be a real
        boolean so a string ``"false"`` cannot silently switch accounting on.
        """
        if "includeLlmUsage" in options and not isinstance(options["includeLlmUsage"], bool):
            raise ValueError(f"Invalid includeLlmUsage {_clip(options['includeLlmUsage'])}; must be a boolean")
        return options

    @field_validator("options", mode="after")
    @classmethod
    def _normalize_dimensions(cls, options: dict) -> dict:
        """Normalize ``options.dimensions`` from the WIRE shape to the internal one.

        The API contract (``serve.smithy``: ``dimensions: DimensionFilterList``)
        declares a LIST of ``DimensionFilter`` objects — ``[{"name", "value",
        "operator"?}, ...]`` — but ``MetricResolver.substitute_dimensions`` binds
        placeholders from a NAME->VALUE mapping. A dict is also accepted and gets the
        same per-entry checks, so it cannot bind ``'None'`` or a Python repr into the
        SQL. A malformed payload raises ``ValueError`` (a 400).
        """
        if "dimensions" not in options:
            return options

        raw = options["dimensions"]
        # Copy: do not mutate the caller's payload.
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
            # operator must be REJECTED, not silently applied as equality. Nothing
            # upstream enforces the ``serve.smithy`` enum, so this check is what
            # rejects. Blank/whitespace counts as ABSENT (a generated client may
            # serialize an unset optional string as "").
            if operator is not None and (not isinstance(operator, str) or operator.strip() not in ("", "=")):
                raise ValueError(f"Unsupported dimension operator {_clip(operator)}; only '=' is supported")
            # A repeated name would collapse to the last value, so reject it.
            # casefold(), not lower(): over-matching is the safe direction for
            # duplicate rejection.
            if name.casefold() in seen:
                raise ValueError(f"Duplicate dimension filter for {_clip(name)}; specify each dimension at most once")
            seen.add(name.casefold())
            normalized[name] = value

        options["dimensions"] = normalized
        return options


class LlmModelUsage(BaseModel):
    """Provider-reported token totals for one model used by the request."""

    model_id: str = Field(..., min_length=1, alias="modelId")
    input_tokens: int = Field(..., ge=0, alias="inputTokens")
    output_tokens: int = Field(..., ge=0, alias="outputTokens")
    max_input_tokens_per_call: int = Field(..., ge=0, alias="maxInputTokensPerCall")
    model_config = {"populate_by_name": True, "strict": True}


class LlmUsageMetrics(BaseModel):
    """Text-LLM usage incurred while serve answered one request.

    Mirrors ``LlmUsageMetrics`` in ``serve.smithy``. Present only when the caller set
    ``options.includeLlmUsage``. ``complete`` is False when any call's usage was
    unknown. Model ids are included so downstream benchmark reporting can apply a
    versioned price table without coupling Serve to AWS pricing.
    """

    input_tokens: int = Field(..., ge=0, alias="inputTokens")
    output_tokens: int = Field(..., ge=0, alias="outputTokens")
    per_model: list[LlmModelUsage] = Field(..., alias="perModel")
    complete: bool
    model_config = {"populate_by_name": True, "strict": True}

    @classmethod
    def from_snapshot(cls, snapshot: LlmUsageSnapshot) -> LlmUsageMetrics:
        """Build the outbound shape from a finalized tracker snapshot."""
        return cls(
            input_tokens=snapshot.input_tokens,
            output_tokens=snapshot.output_tokens,
            per_model=[
                LlmModelUsage(
                    model_id=usage.model_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    max_input_tokens_per_call=usage.max_input_tokens_per_call,
                )
                for usage in snapshot.per_model
            ],
            complete=snapshot.complete,
        )


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
    # Execution mode that produced this result ("standard" / "deep-reasoning"),
    # orthogonal to the tier cascade; the console labels an answer by it.
    execution_mode: str | None = Field(None, alias="executionMode")
    partial: bool = False
    metadata: dict | None = None
    # Opt-in (options.includeLlmUsage): absent unless requested, so the default
    # response shape is unchanged.
    llm_usage: LlmUsageMetrics | None = Field(None, alias="llmUsage")
    model_config = {"populate_by_name": True}

    # The compiled SQL / generated SPARQL as produced, BEFORE the include gates
    # blank ``query_used`` / ``sparql_generated``. Never serialized (private
    # attributes are not fields); read only by the schema-visibility scrub, which
    # needs the withheld identifiers to redact them from the answer and rows.
    _raw_query_used: str | None = PrivateAttr(default=None)
    _raw_sparql: str | None = PrivateAttr(default=None)

    def with_raw_artifacts(self, *, query_used: str | None, sparql: str | None = None) -> QueryResult:
        """Record the ungated SQL/SPARQL for identifier collection; returns ``self``."""
        self._raw_query_used = query_used or None
        self._raw_sparql = sparql or None
        return self

    @property
    def raw_query_used(self) -> str | None:
        """The ungated SQL (falls back to ``query_used``); never part of the response."""
        return self._raw_query_used or self.query_used

    @property
    def raw_sparql(self) -> str | None:
        """The ungated SPARQL (falls back to ``sparql_generated``); never part of the response."""
        return self._raw_sparql or self.sparql_generated


class InvokeResponse(BaseModel):
    """Outbound API envelope wrapping a QueryResult with request/session ids."""

    result: QueryResult
    requestId: str | None = None
    sessionId: str | None = None
