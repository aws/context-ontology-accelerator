# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Response-level schema suppression for schema-blind callers.

ADR: decompose-viewnamespace-schema-visibility, suppression scheme (a)
"results only". A caller that lacks ``viewSource`` must not receive source
schema (table/column names, compiled SQL); a caller that lacks
``viewOntology`` must not receive ontology schema (class/property IRIs,
generated SPARQL). The caller still gets the answer: result rows, the
synthesized answer, confidence and the trace's step names and timings.

Every serve response that can carry schema leaves through one of the
``scrub_*`` functions below — the query result (single and streamed), the
``translate`` action and the ``graphTraverse`` action — so the guarantee
holds for every caller (console, REST data layer, MCP) rather than only for
the MCP tools. Two mechanisms apply:

* **Field blanking.** Each structured schema field (compiled SQL, generated
  SPARQL, table lists, graph context, trace step detail) is withheld whole; an
  emptied field keeps its type so the response shape is unchanged.
* **Per-value redaction.** The identifiers being withheld (db-qualified tables
  from the compiled SQL and table lists; class/predicate IRIs from the graph
  context and the generated SPARQL) are redacted inside free text and values
  the caller still receives: the synthesized answer, string result-row values,
  supporting-content text and labels, and replayed session turns. Without
  ``viewOntology`` every fragment IRI is also redacted from those values.
  Identifiers are collected from the UNGATED artifacts (see
  ``QueryResult.raw_query_used`` / ``raw_sparql``), because the gated fields are
  already blank for exactly the callers this protects.
* **Constant-derived values.** Result columns built from text in the query
  itself have the bare schema names the model was shown redacted, and any found
  there are redacted from the answer too (see ``redact_constant_values``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import sqlglot
import structlog

if TYPE_CHECKING:
    from .models import QueryResult, TraceStep
    from .tier2.sql_firewall import SQLFirewall

logger = structlog.get_logger(__name__)

# True while serving a caller who lacks viewSource or viewOntology. Set by the
# serve entrypoint around query resolution and read by the Tier-3 synthesizer to
# add a "don't reveal identifiers" instruction. A ContextVar so it reaches the
# tasks the query spawns without threading a flag through every signature.
SCHEMA_BLIND: ContextVar[bool] = ContextVar("schema_blind", default=False)

# QueryResult.metadata keys that list physical source tables.
_SOURCE_METADATA_KEYS = ("retrieved_tables", "expanded_tables")


@dataclass(frozen=True)
class SchemaVisibility:
    """What schema the caller may see: source (``viewSource``) and ontology (``viewOntology``)."""

    source: bool
    ontology: bool

    @property
    def full(self) -> bool:
        """Whether the caller may see every kind of schema (nothing to withhold)."""
        return self.source and self.ontology


FULL_VISIBILITY = SchemaVisibility(source=True, ontology=True)
# The fail-closed decision: every kind of schema withheld.
NO_VISIBILITY = SchemaVisibility(source=False, ontology=False)

# The visibility the serve entrypoint resolved for the request being served,
# keyed by its namespace, so the orchestrator's artifact gates and the firewall's
# constant-query check reuse that one decision instead of re-evaluating Cedar on
# the event loop. Unset (None) outside a request: callers then fall back to a
# fail-closed Cedar probe.
_REQUEST_VISIBILITY: ContextVar[tuple[str, SchemaVisibility] | None] = ContextVar(
    "request_schema_visibility", default=None
)


def request_schema_visibility(namespace: str) -> SchemaVisibility | None:
    """The visibility resolved for this request on ``namespace``, or None when unset/other namespace."""
    current = _REQUEST_VISIBILITY.get()
    if current is None or current[0] != namespace:
        return None
    return current[1]


@contextmanager
def schema_visibility_scope(namespace: str, vis: SchemaVisibility) -> Iterator[None]:
    """Publish ``vis`` (and ``SCHEMA_BLIND``) for the duration of one request.

    Tasks created inside the scope inherit the values. Both are reset on exit;
    a reset from a different context (an async generator closed elsewhere) is
    skipped, since the values then belong to a context that is going away.
    """
    blind_token = SCHEMA_BLIND.set(not vis.full)
    vis_token = _REQUEST_VISIBILITY.set((namespace, vis))
    # One mutable set per request, created before any task is spawned, so names
    # noted inside child tasks land in the same set the scrub reads.
    names_token = _PROMPT_SCHEMA_NAMES.set(None if vis.source else set())
    try:
        yield
    finally:
        try:
            _PROMPT_SCHEMA_NAMES.reset(names_token)
            _REQUEST_VISIBILITY.reset(vis_token)
            SCHEMA_BLIND.reset(blind_token)
        except ValueError:
            logger.debug("schema_visibility_scope_reset_skipped")


# ── Schema names a model was shown (constant-smuggling defence) ─────────────

# Bare table, column and database names that were put in a model prompt, or
# returned to an agent by a schema tool, while serving a caller without
# viewSource. None outside such a request, so producers pay nothing for a
# caller who may see source schema. A model that has seen these names can write
# them into a string constant beside a real aggregate
# (``SELECT COUNT(*), 'customers, invoices' FROM orders``), which no SQL-shape
# rule refuses; the scrub redacts them from constant-derived result values and
# from the answer instead.
_PROMPT_SCHEMA_NAMES: ContextVar[set[str] | None] = ContextVar("prompt_schema_names", default=None)

# Schema lines are rendered as ``Table: t [(label: …)] | Database: db (…) |
# Columns: c:type [label: …] (desc), …`` (class text and graph walks alike);
# agent tools return JSON with ``"table"`` / ``"column"`` keys.
_TABLE_SEGMENT_RE = re.compile(r"\b(?:Table|Database):\s*([A-Za-z_][\w$]*)")
_COLUMNS_SEGMENT_RE = re.compile(r"\bColumns:\s*([^|\n]*)")
_COLUMN_ENTRY_RE = re.compile(r"(?:^|,)\s*([A-Za-z_][\w$]*)\s*:")
_JSON_NAME_RE = re.compile(r'"(?:table|column)"\s*:\s*"([A-Za-z_][\w$.]*)"')
# Names this short are too ambiguous to redact as whole tokens (``id``, ``ts``).
_MIN_SCHEMA_NAME_LEN = 3
# Above this many names, constant-derived values are withheld whole rather than
# matched name by name (bounded cost, still closed).
_MAX_SCHEMA_NAMES = 5000


def schema_names_in_text(text: str) -> set[str]:
    """Bare table, database and column names in schema text a model is shown (lower-cased)."""
    names: set[str] = set(_TABLE_SEGMENT_RE.findall(text))
    for segment in _COLUMNS_SEGMENT_RE.findall(text):
        names.update(_COLUMN_ENTRY_RE.findall(segment))
    for name in _JSON_NAME_RE.findall(text):
        names.update(name.split("."))
    return {n.lower() for n in names if len(n) >= _MIN_SCHEMA_NAME_LEN}


def note_prompt_schema(*texts: str | None) -> None:
    """Record the schema names in ``texts`` for this request's scrub (no-op unless source is withheld)."""
    acc = _PROMPT_SCHEMA_NAMES.get()
    if acc is None:
        return
    for text in texts:
        if text:
            acc |= schema_names_in_text(text)


def prompt_schema_names() -> frozenset[str]:
    """The names noted so far in this request (empty outside a source-withheld request)."""
    acc = _PROMPT_SCHEMA_NAMES.get()
    return frozenset(acc) if acc else frozenset()


def resolve_schema_visibility(firewall: SQLFirewall, namespace: str, profile: dict[str, Any]) -> SchemaVisibility:
    """Evaluate both rights once through the firewall's fail-closed Cedar probes."""
    return SchemaVisibility(
        source=firewall.can_view_source(namespace, profile),
        ontology=firewall.can_view_ontology(namespace, profile),
    )


def scrub_trace_step(step: TraceStep | dict, vis: SchemaVisibility) -> TraceStep | dict:
    """Drop a trace step's ``detail`` unless the caller may see all schema.

    Step details carry table names (``t2.sql.generate``, ``t2.vkg.compile``),
    ontology edge names and LLM reasoning text (Tier 3), so a caller missing
    either right keeps the step name, status and timings but not the detail.
    """
    if vis.full:
        return step
    if isinstance(step, dict):
        return {k: v for k, v in step.items() if k != "detail"}
    return step.model_copy(update={"detail": None})


def scrub_trace(steps: list | None, vis: SchemaVisibility) -> list | None:
    """Apply :func:`scrub_trace_step` to every step."""
    if steps is None or vis.full:
        return steps
    return [scrub_trace_step(s, vis) for s in steps]


def scrub_query_result(result: QueryResult, vis: SchemaVisibility) -> QueryResult:
    """Withhold the schema-bearing fields of a query result (mutated in place).

    Besides blanking the structured fields, the identifiers being withheld are
    redacted from the answer text, from string values in the result rows and
    from supporting-content text (see the module docstring). The identifiers
    come from the ungated SQL/SPARQL, so they are found even though the gated
    fields are already blank for this caller.
    """
    if vis.full:
        return result
    redactor = _redactor_for(_identifier_view(result), vis)
    # Gathered before the source fields below are blanked.
    constant_names = (
        _constant_redaction_names(result.raw_query_used, result.metadata, prompt_schema_names())
        if not vis.source
        else frozenset()
    )
    withheld: list[str] = []
    if not vis.source:
        if result.query_used:
            result.query_used = None
            withheld.append("queryUsed")
        if result.metadata:
            for key in _SOURCE_METADATA_KEYS:
                if result.metadata.get(key):
                    result.metadata[key] = []
                    withheld.append(f"metadata.{key}")
    if not vis.ontology:
        if result.sparql_generated:
            result.sparql_generated = None
            withheld.append("sparqlGenerated")
        if result.graph_context and result.graph_context != _EMPTY_GRAPH_CONTEXT:
            result.graph_context = {"entities": []}
            withheld.append("graphContext")
    if result.trace:
        result.trace = scrub_trace(result.trace, vis)
        withheld.append("trace.detail")
    smuggled: set[str] = set()
    if result.result_rows:
        rows = redactor.rows(result.result_rows)
        rows, smuggled = redact_constant_values(rows, result.raw_query_used, constant_names)
        if rows != result.result_rows:
            result.result_rows = rows
            withheld.append("resultRows")
    smuggled_pattern = _name_pattern(smuggled)
    if result.synthesized_answer:
        redacted = redactor.answer(result.synthesized_answer)
        if smuggled_pattern is not None:
            redacted = smuggled_pattern.sub(_REDACTED, redacted)
        if redacted != result.synthesized_answer:
            result.synthesized_answer = redacted
            withheld.append("synthesizedAnswer")
    if result.supporting_content:
        supporting = redactor.supporting(result.supporting_content)
        if smuggled_pattern is not None:
            supporting = _redact_supporting_names(supporting, smuggled_pattern)
        if supporting != result.supporting_content:
            result.supporting_content = supporting
            withheld.append("supportingContent")
    if withheld:
        logger.info(
            "schema_fields_withheld",
            surface="query",
            fields=withheld,
            overflow=redactor.overflow,
            constant_names_redacted=len(smuggled),
        )
    return result


def _redact_supporting_names(chunks: list, pattern: re.Pattern[str]) -> list:
    out: list = []
    for chunk in chunks:
        if isinstance(chunk, dict):
            chunk = {
                k: pattern.sub(_REDACTED, v) if k in ("text", "label") and isinstance(v, str) else v
                for k, v in chunk.items()
            }
        out.append(chunk)
    return out


def _identifier_view(result: QueryResult) -> dict[str, Any]:
    """Only the fields identifiers are collected from, with the UNGATED SQL/SPARQL (no full dump)."""
    return {
        "queryUsed": result.raw_query_used,
        "sparqlGenerated": result.raw_sparql,
        "graphContext": result.graph_context,
        "metadata": result.metadata,
    }


# ── Identifier redaction (answer text, rows, supporting content, replay) ────

# An IRI, optionally in SPARQL angle brackets.
_IRI_RE = re.compile(r"<?(https?://[^\s<>\"')\]]+)>?")
# A dotted SQL identifier (catalog.schema.table or schema.table); starts with a letter.
_QUALIFIED_NAME_RE = re.compile(r"\b[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*){1,2}\b")
# A local name that is clearly an identifier, not a word: has an underscore or an inner capital.
_IDENTIFIER_LIKE_RE = re.compile(r"^(?:[A-Za-z]+_[\w]+|[a-z]+[A-Z]\w*|[A-Z][a-z]+[A-Z]\w*)$")
# A fragment IRI (``https://…#Name``) — the form ontology classes and properties
# take. Redacted wholesale from text shown to a caller without viewOntology,
# since the planner/synthesis prompts can carry ontology terms that never reach
# the structured result (and so are not in the collected identifier set).
_FRAGMENT_IRI_RE = re.compile(r"<?https?://[^\s<>\"')\]#]+#[A-Za-z_][\w.-]*>?")
# A supporting-content chunk id derived from a graph node (``<node>#stmt<n>``)
# or carrying an IRI: it names the node, so a blind caller gets an opaque id.
_GRAPH_CHUNK_ID_RE = re.compile(r"#stmt\d+$|://")
_REDACTED = "[redacted]"
_EMPTY_GRAPH_CONTEXT: dict[str, list] = {"entities": []}
# Above this many identifiers, per-value redaction is not attempted: the answer,
# rows and supporting content are withheld whole (bounded cost, still closed).
_MAX_IDENTIFIERS = 500
_WITHHELD_ANSWER = "[answer withheld: it referenced schema details you do not have access to]"


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def _iri_identifiers(text: str) -> set[str]:
    out: set[str] = set()
    for iri in _IRI_RE.findall(text):
        iri = iri.rstrip(".,;")
        out.add(iri)
        local = re.split(r"[#/]", iri)[-1]
        if local and _IDENTIFIER_LIKE_RE.match(local):
            out.add(local)
    return out


def _graph_schema_terms(graph_context: Any) -> list[str]:
    """Class ``type`` and relationship ``predicate`` IRIs of a graph context.

    Entity ``uri`` / relationship ``target_uri`` are instance IRIs — data, not
    schema — so they are deliberately not collected (an instance local name such
    as ``Order_1001`` is a legitimate value the caller may see).
    """
    if not isinstance(graph_context, Mapping):
        return []
    terms: list[str] = []

    def _add_relationships(rels: Any) -> None:
        for rel in rels or []:
            if isinstance(rel, str):
                terms.append(rel)
            elif isinstance(rel, Mapping):
                for key in ("predicateUri", "predicate_uri", "predicate"):
                    terms.extend(_strings(rel.get(key)))

    for entity in graph_context.get("entities") or []:
        if not isinstance(entity, Mapping):
            continue
        terms += _strings(entity.get("type"))
        # Older producers nest relationships under each entity.
        _add_relationships(entity.get("relationships"))
    # The canonical graph context (response_assembler) lists them at the top level.
    _add_relationships(graph_context.get("relationships"))
    return terms


def _collect(result: Mapping[str, Any], vis: SchemaVisibility) -> tuple[set[str], set[str]]:
    """(source identifiers, ontology identifiers) that ``vis`` withholds, from a camelCase view."""
    source: set[str] = set()
    ontology: set[str] = set()
    if not vis.ontology:
        for text in _graph_schema_terms(result.get("graphContext")) + _strings(result.get("sparqlGenerated")):
            ontology |= _iri_identifiers(text)
    if not vis.source:
        sql = result.get("queryUsed")
        if isinstance(sql, str):
            source |= {m for m in _QUALIFIED_NAME_RE.findall(sql) if not m.replace(".", "").isdigit()}
        metadata = result.get("metadata")
        if isinstance(metadata, Mapping):
            for key in _SOURCE_METADATA_KEYS:
                for table in _strings(metadata.get(key)):
                    if "." in table or _IDENTIFIER_LIKE_RE.match(table):
                        source.add(table)
    return {f for f in source if len(f) > 2}, {f for f in ontology if len(f) > 2}


def collect_withheld_identifiers(result: Mapping[str, Any], vis: SchemaVisibility) -> set[str]:
    """Schema identifiers present in a (camelCase) result that ``vis`` withholds.

    Ontology identifiers come from the graph context's class/predicate IRIs and
    the generated SPARQL; source identifiers from the compiled SQL and the
    retrieved/expanded table lists. Only unmistakable identifiers are collected
    (full IRIs, identifier-like IRI local names, dotted table names), so plain
    words in an answer are never redacted.
    """
    source, ontology = _collect(result, vis)
    return source | ontology


def _compile(source: set[str], ontology: set[str]) -> re.Pattern[str] | None:
    """One alternation over every identifier, longest first (so the longest match wins).

    SQL identifiers are case-insensitive, so source identifiers match in any
    case; a word-like identifier only matches as a whole token.
    """
    alternatives: list[str] = []
    for ident in sorted(source | ontology, key=lambda i: (-len(i), i)):
        alt = re.escape(ident)
        if ident in source:
            alt = f"(?i:{alt})"
        if re.match(r"\w", ident):
            alt = rf"(?<![\w.]){alt}(?!\w)"
        alternatives.append(alt)
    return re.compile("|".join(alternatives)) if alternatives else None


@dataclass(frozen=True)
class _Redactor:
    """Redacts withheld identifiers (and, without viewOntology, fragment IRIs) from values."""

    pattern: re.Pattern[str] | None
    fragment_iris: bool
    overflow: bool = False

    def text(self, text: str) -> str:
        if self.pattern is not None:
            text = self.pattern.sub(_REDACTED, text)
        if self.fragment_iris:
            text = _FRAGMENT_IRI_RE.sub(_REDACTED, text)
        return text

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v) for v in value]
        return value

    def answer(self, text: str) -> str:
        if self.overflow:
            return _WITHHELD_ANSWER
        return self.text(text)

    def rows(self, rows: list) -> list:
        if self.overflow:
            return []
        if self.pattern is None and not self.fragment_iris:
            return rows
        return [self.value(row) for row in rows]

    def supporting(self, chunks: list) -> list:
        if self.overflow:
            return []
        out: list = []
        for idx, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                out.append(chunk)
                continue
            clean = dict(chunk)
            for key in ("text", "label"):
                if isinstance(clean.get(key), str):
                    clean[key] = self.text(clean[key])
            chunk_id = clean.get("chunkId")
            if self.fragment_iris and isinstance(chunk_id, str) and _GRAPH_CHUNK_ID_RE.search(chunk_id):
                clean["chunkId"] = f"graph-chunk-{idx}"
            out.append(clean)
        return out


def _redactor_for(view: Mapping[str, Any], vis: SchemaVisibility) -> _Redactor:
    source, ontology = _collect(view, vis)
    if len(source) + len(ontology) > _MAX_IDENTIFIERS:
        return _Redactor(pattern=None, fragment_iris=not vis.ontology, overflow=True)
    return _Redactor(pattern=_compile(source, ontology), fragment_iris=not vis.ontology)


def redact_identifiers(text: str, identifiers: set[str]) -> str:
    """Replace each identifier in ``text`` (longest first) with ``[redacted]``."""
    pattern = _compile(set(), set(identifiers))
    return pattern.sub(_REDACTED, text) if pattern is not None else text


def redact_answer_text(text: str, identifiers: set[str], vis: SchemaVisibility) -> str:
    """Redact withheld identifiers, plus every fragment IRI when ontology is withheld."""
    if vis.full or not text:
        return text
    return _Redactor(pattern=_compile(set(), set(identifiers)), fragment_iris=not vis.ontology).text(text)


# ── Constant-derived result values (source schema withheld) ─────────────────
#
# The SQL firewall refuses a statement that can only echo text (no table, or
# only constants), but one data-dependent projection beside a constant passes:
# ``SELECT COUNT(*) AS n, 'customers, invoices' AS t FROM orders``. Rather than
# more shape rules, every value of a result column whose projection carries
# constant text (a string literal, or a function of no column such as
# ``CHR(99)``) has the bare schema names the model was shown redacted as whole
# tokens. Data columns are untouched, so a value that happens to equal a column
# name is only redacted when the statement itself could have put it there. The
# names actually found are then redacted from the answer and supporting content
# too, since the synthesizer read the rows before this scrub.

# Dialects tried in order when locating projections; the first clean parse wins.
_PROJECTION_DIALECTS: tuple[str | None, ...] = (None, "athena", "redshift", "postgres")


def _parse_sql(sql: str) -> sqlglot.exp.Expression | None:
    for dialect in _PROJECTION_DIALECTS:
        try:
            parsed = sqlglot.parse_one(sql, read=dialect)
        except sqlglot.errors.SqlglotError:
            continue
        if parsed is not None:
            return parsed
    return None


def _select_branches(node: sqlglot.exp.Expression) -> list[sqlglot.exp.Expression]:
    if isinstance(node, sqlglot.exp.SetOperation):
        return _select_branches(node.left) + _select_branches(node.right)
    if isinstance(node, sqlglot.exp.Subquery):
        return _select_branches(node.this)
    return [node]


def _carries_constant_text(projection: sqlglot.exp.Expression) -> bool:
    for node in projection.walk():
        if isinstance(node, sqlglot.exp.Literal) and node.is_string:
            return True
        if (
            isinstance(node, sqlglot.exp.Func)
            and not isinstance(node, sqlglot.exp.AggFunc)
            and node.find(sqlglot.exp.Column) is None
        ):
            return True
    return False


def _is_star(projection: sqlglot.exp.Expression) -> bool:
    return isinstance(projection, sqlglot.exp.Star) or (
        isinstance(projection, sqlglot.exp.Column) and isinstance(projection.this, sqlglot.exp.Star)
    )


@dataclass(frozen=True)
class _ConstantColumns:
    """Where a statement's constant text lands in its result.

    ``positions`` None means a constant projection exists but cannot be mapped
    onto row keys (unparseable SQL, a ``*`` beside it, or UNION branches of
    different width): every column is then treated as constant-derived (fail
    closed). ``width`` is the projection count used to check the mapping.
    """

    positions: frozenset[int] | None
    width: int = -1

    @property
    def none(self) -> bool:
        return self.positions is not None and not self.positions

    def targets(self, count: int) -> set[int]:
        if self.positions is None or count != self.width:
            return set(range(count))
        return set(self.positions)


def _constant_columns(sql: str) -> _ConstantColumns:
    """Output positions whose projection carries constant text (see :class:`_ConstantColumns`)."""
    parsed = _parse_sql(sql)
    if parsed is None:
        return _ConstantColumns(positions=None)
    positions: set[int] = set()
    widths: set[int] = set()
    star = False
    for branch in _select_branches(parsed):
        if not isinstance(branch, sqlglot.exp.Select):
            return _ConstantColumns(positions=None)
        widths.add(len(branch.expressions))
        for idx, projection in enumerate(branch.expressions):
            if _is_star(projection):
                star = True
            elif _carries_constant_text(projection):
                positions.add(idx)
    if not positions:
        return _ConstantColumns(positions=frozenset())
    if star or len(widths) != 1:
        return _ConstantColumns(positions=None)
    return _ConstantColumns(positions=frozenset(positions), width=widths.pop())


def _sql_referenced_names(parsed: sqlglot.exp.Expression | None) -> set[str]:
    if parsed is None:
        return set()
    names = {t.name for t in parsed.find_all(sqlglot.exp.Table)}
    names |= {t.db for t in parsed.find_all(sqlglot.exp.Table)}
    names |= {c.name for c in parsed.find_all(sqlglot.exp.Column)}
    return {n.lower() for n in names if n and len(n) >= _MIN_SCHEMA_NAME_LEN}


def _constant_redaction_names(sql: Any, metadata: Any, prompt_names: frozenset[str]) -> frozenset[str]:
    """Bare names to redact from constant-derived values: shown to the model, in the SQL, or listed."""
    names = set(prompt_names)
    if isinstance(sql, str) and sql:
        names |= _sql_referenced_names(_parse_sql(sql))
    if isinstance(metadata, Mapping):
        for key in _SOURCE_METADATA_KEYS:
            for table in _strings(metadata.get(key)):
                names |= {part.lower() for part in table.split(".") if len(part) >= _MIN_SCHEMA_NAME_LEN}
    return frozenset(names)


def _name_pattern(names: set[str] | frozenset[str]) -> re.Pattern[str] | None:
    """Whole-token, case-insensitive match of any of ``names`` (also inside dotted names)."""
    if not names:
        return None
    alternation = "|".join(re.escape(n) for n in sorted(names, key=lambda n: (-len(n), n)))
    return re.compile(rf"(?i)(?<![\w$])({alternation})(?![\w$])")


def redact_constant_values(rows: list, sql: Any, names: frozenset[str]) -> tuple[list, set[str]]:
    """Redact ``names`` from constant-derived result columns; returns (rows, names found)."""
    if not rows or not names or not isinstance(sql, str) or not sql:
        return rows, set()
    columns = _constant_columns(sql)
    if columns.none:
        return rows, set()
    overflow = len(names) > _MAX_SCHEMA_NAMES
    pattern = None if overflow else _name_pattern(names)
    found: set[str] = set()

    def _sub(match: re.Match[str]) -> str:
        found.add(match.group(1).lower())
        return _REDACTED

    def _clean(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if pattern is None:
            return _REDACTED
        return pattern.sub(_sub, value)

    out: list = []
    for row in rows:
        if isinstance(row, dict):
            targets = columns.targets(len(row))
            out.append({k: _clean(v) if i in targets else v for i, (k, v) in enumerate(row.items())})
        elif isinstance(row, list):
            targets = columns.targets(len(row))
            out.append([_clean(v) if i in targets else v for i, v in enumerate(row)])
        else:
            out.append(row)
    return out, found


def _scrub_turn_metadata(
    meta: dict, vis: SchemaVisibility, redactor: _Redactor, smuggled: set[str] | None = None
) -> dict:
    """The replay twin of :func:`scrub_query_result`, on a stored camelCase result.

    Applies the same field rules, so a replayed turn serializes exactly as the
    live response would have (see the parity test). A stored turn does not
    record what the model was shown, so constant-derived values are redacted
    with the names in its stored SQL and table lists; names found are added to
    ``smuggled`` for the caller to redact from the turn's text.
    """
    smuggled = smuggled if smuggled is not None else set()
    constant_names = (
        _constant_redaction_names(meta.get("queryUsed"), meta.get("metadata"), frozenset())
        if not vis.source
        else frozenset()
    )
    stored_sql = meta.get("queryUsed")
    clean = dict(meta)
    if not vis.source:
        clean.pop("queryUsed", None)
        if isinstance(clean.get("metadata"), dict):
            inner = dict(clean["metadata"])
            for key in _SOURCE_METADATA_KEYS:
                if inner.get(key):
                    inner[key] = []
            clean["metadata"] = inner
    if not vis.ontology:
        clean.pop("sparqlGenerated", None)
        if clean.get("graphContext"):
            clean["graphContext"] = {"entities": []}
    if isinstance(clean.get("trace"), list):
        clean["trace"] = scrub_trace(clean["trace"], vis)
    if isinstance(clean.get("resultRows"), list):
        rows = redactor.rows(clean["resultRows"])
        rows, found = redact_constant_values(rows, stored_sql, constant_names)
        smuggled |= found
        clean["resultRows"] = rows
    smuggled_pattern = _name_pattern(smuggled)
    if isinstance(clean.get("synthesizedAnswer"), str) and clean["synthesizedAnswer"]:
        answer = redactor.answer(clean["synthesizedAnswer"])
        clean["synthesizedAnswer"] = smuggled_pattern.sub(_REDACTED, answer) if smuggled_pattern else answer
    if isinstance(clean.get("supportingContent"), list):
        supporting = redactor.supporting(clean["supportingContent"])
        if smuggled_pattern is not None:
            supporting = _redact_supporting_names(supporting, smuggled_pattern)
        clean["supportingContent"] = supporting
    return clean


def scrub_session_messages(messages: list[dict], vis: SchemaVisibility) -> list[dict]:
    """Re-scrub replayed session turns with the caller's CURRENT rights.

    Turns are stored as they were returned. A turn stored before this scrub
    existed, or while the caller held a schema-visible role, can still carry
    schema, so replay applies the live field rules to each assistant turn's
    stored result (``metadata``) and redacts the withheld identifiers from its
    text (``content``). An assistant turn with no stored result (legacy history)
    still has its text redacted (fragment IRIs without ``viewOntology``).
    """
    if vis.full:
        return messages
    out: list[dict] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            out.append(msg)
            continue
        meta = msg.get("metadata")
        redactor = _redactor_for(meta if isinstance(meta, dict) else {}, vis)
        turn = dict(msg)
        smuggled: set[str] = set()
        if isinstance(meta, dict):
            turn["metadata"] = _scrub_turn_metadata(meta, vis, redactor, smuggled)
        content = msg.get("content")
        if isinstance(content, str) and content:
            text = redactor.answer(content)
            smuggled_pattern = _name_pattern(smuggled)
            turn["content"] = smuggled_pattern.sub(_REDACTED, text) if smuggled_pattern else text
        out.append(turn)
    return out


def scrub_translate_response(response: dict, vis: SchemaVisibility) -> dict:
    """Withhold the generated SPARQL from a ``translate`` response without ``viewOntology``."""
    if vis.full or "error" in response:
        return response
    out = dict(response)
    if not vis.ontology and out.get("sparqlQuery"):
        out["sparqlQuery"] = ""
    if "trace" in out:
        out["trace"] = scrub_trace(out["trace"], vis)
    logger.info("schema_fields_withheld", surface="translate", ontology=vis.ontology, source=vis.source)
    return out


def scrub_graph_traverse_response(response: dict, vis: SchemaVisibility) -> dict:
    """Withhold entities/relationships from a ``graphTraverse`` response without ``viewOntology``."""
    if vis.full or "error" in response:
        return response
    out = dict(response)
    if not vis.ontology:
        if "entities" in out:
            out["entities"] = []
        if "relationships" in out:
            out["relationships"] = []
    if "trace" in out:
        out["trace"] = scrub_trace(out["trace"], vis)
    logger.info("schema_fields_withheld", surface="graphTraverse", ontology=vis.ontology, source=vis.source)
    return out


# Client-visible ServeError types whose message can name physical schema
# (tables, catalogs, schemas, datasource ids) and the generic text a caller
# without the matching right receives instead.
_SCHEMA_BEARING_ERRORS: dict[str, str] = {
    "AmbiguousReferenceError": (
        "The question matches data in more than one source, so it can't be answered as asked. "
        "Ask a more specific question, or ask a data steward to narrow the namespace."
    ),
}


def client_error_message(error_type: str, message: str, vis: SchemaVisibility) -> str:
    """The message to return for a client-visible serve error.

    An error whose text names source schema (e.g. ``AmbiguousReferenceError``,
    which lists the ambiguous table and its candidate ``datasource/schema``
    pairs) is replaced with generic guidance unless the caller holds
    ``viewSource``. The original text is still logged server-side.
    """
    if vis.source or error_type not in _SCHEMA_BEARING_ERRORS:
        return message
    return _SCHEMA_BEARING_ERRORS[error_type]
