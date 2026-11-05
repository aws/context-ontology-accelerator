# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SPARQLFirewall — the safety gate between generated SPARQL and a graph endpoint.

The SPARQL analogue of ``tier2/sql_firewall.py``: the LLM-generated query is the
untrusted artefact, so nothing reaches a source's data endpoint until it passes
here. Engine-agnostic — it works on the parsed query, not on transport — so every
``SparqlEndpointClient`` inherits the same guarantees. Distinct from
``SPARQLValidator``, which checks correctness (syntax, URI existence,
determinism), not safety.

Rules (LLD §2.2.3):

* **F1 read-only form** — the query must parse under the SPARQL *query* grammar as
  SELECT, ASK, CONSTRUCT or DESCRIBE. Every update form (INSERT, DELETE, DROP,
  CREATE, LOAD, CLEAR, COPY, MOVE, ADD), and a query with an update smuggled after
  ``;``, fails to parse. Parsing rather than keyword matching means a keyword inside
  an IRI, literal or comment neither false-positives nor slips through.
* **F2 named-graph scoping** — the graph a query may read is the one the source
  declares in ``configuration.dataNamedGraph``. Caller-written ``FROM`` /
  ``FROM NAMED`` are always rejected. When the source declares a graph, a
  ``GRAPH`` block may only name that graph, and the query is confined to it with
  ``FROM <g> FROM NAMED <g>``; when it does not, the A-Box is the default graph and any ``GRAPH`` block is rejected. The
  firewall never guesses a graph IRI.
* **F3 result bound** — a SELECT / CONSTRUCT always carries a top-level ``LIMIT`` no
  larger than the cap (``DEFAULT_MAX_RESULTS``, lowered by the caller's
  ``maxResults``): one is injected when absent and lowered when larger. ASK has no
  solution sequence to bound. DESCRIBE is rejected: a LIMIT bounds the resources it
  describes, never the triples it returns, so it cannot be bounded.
* **F4 fail closed** — a parse failure, an unrecognised form, or a rewrite that does
  not re-parse to exactly the intended dataset and bound is rejected. So is a query
  whose text, parse tree and algebra disagree on its graph patterns (GRAPH, SERVICE,
  OPTIONAL, MINUS, FILTER, EXISTS): rdflib's algebra drops a FILTER nested in an
  EXISTS group, but the endpoint runs the text.
* **F5 no federation** — a ``SERVICE`` clause (a query reaching out to another
  endpoint) is rejected wherever it appears — WHERE, OPTIONAL, UNION, MINUS,
  ``FILTER EXISTS`` or a subquery — whether its endpoint is an IRI or a variable,
  and with or without ``SILENT``. Federated queries are not supported yet.
* **F6 work bound** — a LIMIT bounds rows, not the work the engine does. When the
  caller gives ``query_timeout_ms``, the query's WHERE group carries Neptune's
  ``queryTimeout`` hint, so the store aborts the query itself. The query may name no
  Neptune hint of its own, so this one is the only one; the short ``CONSTRUCT WHERE``
  form, whose pattern is its template, is refused.

F2 and F5 are each checked three ways, and any one refuses: on the algebra, on the
parse tree, and on the query text itself (a ``SERVICE`` keyword anywhere; a
``GRAPH`` keyword not followed by exactly the source's graph IRI).

Cross-tenant isolation is NOT this module's job: it rests on each source having its
own endpoint (one client per source). F2 confines a query within that endpoint.
Class/predicate-level authorization is deferred (LLD §6).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

import structlog
from coa_common.sparql_config import NAMED_GRAPH_IRI_RE
from pyparsing import ParseResults
from rdflib import BNode, Literal, URIRef, Variable
from rdflib.paths import AlternativePath, InvPath, MulPath, NegatedPath, SequencePath
from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.algebra import translatePName, translatePrologue, traverse
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue

from ...clients.neptune import DEFAULT_MAX_RESULTS

logger = structlog.get_logger(__name__)


class QueryForm(StrEnum):
    """The read-only SPARQL query forms the firewall admits (F1)."""

    SELECT = "SelectQuery"
    ASK = "AskQuery"
    CONSTRUCT = "ConstructQuery"
    DESCRIBE = "DescribeQuery"


class FirewallRule(StrEnum):
    """Which rule rejected a query; carried on the error for traces and metrics."""

    READ_ONLY = "F1_read_only"
    GRAPH_SCOPE = "F2_graph_scope"
    RESULT_BOUND = "F3_result_bound"
    FAIL_CLOSED = "F4_fail_closed"
    FEDERATED = "F5_federated"
    WORK_BOUND = "F6_work_bound"
    GRANT = "F7_grant"


class UnsafeSPARQLError(ValueError):
    """The query was rejected; nothing may be executed."""

    def __init__(self, rule: FirewallRule, message: str):
        """Record the violated rule alongside the message."""
        super().__init__(message)
        self.rule = rule


@dataclass(frozen=True)
class SafeQuery:
    """A query that passed the firewall, in the exact text to execute."""

    sparql: str
    form: QueryForm
    # Top-level LIMIT the query now carries; ``None`` for ASK, which has no rows.
    limit: int | None
    # SELECT projection, in query order — the result's column order (every in-scope
    # variable for ``SELECT *``). Empty for the other forms.
    variables: tuple[str, ...] = ()


_IRIREF_RE = re.compile(r"<[^<>\"{}|^`\\\x00-\x20]*>")
# Not a prefix label (``construct:``) or a local name (``ex:select``).
_FORM_RE = re.compile(r"(?<![\w:])(SELECT|ASK|CONSTRUCT|DESCRIBE)\b(?!:)", re.IGNORECASE)
# A codepoint escape anywhere: the parser expands it before tokenizing, so the text is not what it says.
_ESCAPE_RE = re.compile(r"\\[uU]")
# A keyword preceded by ``?`` / ``$`` is a variable name, not a keyword.
_WHERE_RE = re.compile(r"(?<![?$\w])WHERE\b", re.IGNORECASE)
_VALUES_RE = re.compile(r"(?<![?$\w])VALUES\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"(?<![?$\w])LIMIT\s+(\d+)", re.IGNORECASE)

NEPTUNE_HINTS = "http://aws.amazon.com/neptune/vocab/v01/QueryHints#"
_HINT_TEXT_RE = re.compile(re.escape("aws.amazon.com/neptune/vocab"), re.IGNORECASE)


# A variable or a prefixed name (``ex:sub-graph``, ``ex:a.exists``, ``graph-x:p``); a local name's dot is
# followed by another name character, so the dot that ends a triple (``?o.SERVICE``) is not part of it.
_NAME_RE = re.compile(r"[?$]\w+|(?:[^\W\d][\w.\-]*)?:(?:[\w\-:%]|\\.|\.(?=[\w\-:%]))*")


def _blank_names(masked: str) -> str:
    """``masked`` with every variable and prefixed name blanked, preserving offsets: a keyword in them is no pattern."""
    return _NAME_RE.sub(lambda m: " " * len(m.group()), masked)


def _keyword_re(word: str) -> re.Pattern[str]:
    """``word`` as a keyword, searched in text whose literals, IRIs, comments and names are blanked."""
    return re.compile(rf"(?<!\w){word}(?!\w)", re.IGNORECASE)


_SERVICE_RE = _keyword_re("SERVICE")
_GRAPH_RE = _keyword_re("GRAPH")
# Each graph-pattern keyword and the parse-tree nodes it opens; the text and the tree must agree on the counts.
_PATTERN_NODES: dict[str, tuple[re.Pattern[str], tuple[str, ...]]] = {
    "GRAPH": (_GRAPH_RE, ("GraphGraphPattern",)),
    "SERVICE": (_SERVICE_RE, ("ServiceGraphPattern",)),
    "OPTIONAL": (_keyword_re("OPTIONAL"), ("OptionalGraphPattern",)),
    "MINUS": (_keyword_re("MINUS"), ("MinusGraphPattern",)),
    "FILTER": (_keyword_re("FILTER"), ("Filter",)),
    "EXISTS": (_keyword_re("EXISTS"), ("Builtin_EXISTS", "Builtin_NOTEXISTS")),
}


def _mask(sparql: str) -> str:
    """Blank the contents of string literals, IRIs and comments, preserving offsets.

    Every structural search below runs on the masked text, so a brace, keyword or
    ``#`` inside a literal or IRI can never be mistaken for query structure.
    """
    return _lex(sparql)[0]


def _mask_iri(m: re.Match[str], out: list[str]) -> int:
    """Blank an IRI candidate's contents in ``out``; its end offset."""
    if "'" in m.group():
        # In ``?x<xsd:string('>')`` the parser reads a comparison and a string literal.
        raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "An apostrophe inside <...> is not allowed")
    for k in range(m.start() + 1, m.end() - 1):
        out[k] = " "
    return m.end()


def _lex(sparql: str) -> tuple[str, list[tuple[int, int]]]:
    """The masked text and the masker's comment spans (``#`` up to, not including, its newline).

    The masker is only a guide for where a rewrite goes: what is executed is decided by
    the parser (``_parser_reads_a_comment`` and the F4 re-check of the final text).
    """
    out = list(sparql)
    comments: list[tuple[int, int]] = []
    i, n = 0, len(sparql)
    while i < n:
        ch = sparql[i]
        if ch in "\"'":
            quote = sparql[i : i + 3] if sparql[i : i + 3] in ('"""', "'''") else ch
            j = i + len(quote)
            while j < n and not sparql.startswith(quote, j):
                j += 2 if sparql[j] == "\\" else 1
            end = min(j + len(quote), n)
            for k in range(i + len(quote), min(j, n)):
                out[k] = " "
            i = end
        elif ch == "<" and (m := _IRIREF_RE.match(sparql, i)):
            i = _mask_iri(m, out)
        elif ch == "\\":
            i += 2
        elif ch == "#":
            j = sparql.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            comments.append((i, j))
            i = j
        else:
            i += 1
    return "".join(out), comments


# ``~`` is valid inside an IRI, a literal and an escaped local name, and a syntax error anywhere else.
_COMMENT_PROBE = "~"


def _without_comments(sparql: str) -> str:
    """``sparql`` with every comment the masker sees cut out (each newline kept)."""
    kept, prev = [], 0
    for start, end in _lex(sparql)[1]:
        kept.append(sparql[prev:start])
        prev = end
    kept.append(sparql[prev:])
    return "".join(kept).strip()


def _parses(sparql: str) -> bool:
    try:
        parseQuery(sparql)
    except Exception:
        return False
    return True


def _parser_reads_a_comment(sparql: str) -> bool:
    """Whether the parser reads a comment: a ``#`` it does not keep inside an IRI, a literal or a local name.

    Every ``#`` becomes ``~`` at once. Inside those tokens the text still parses; a ``#`` that opens a
    comment turns into a syntax error.
    """
    return "#" in sparql and not _parses(sparql.replace("#", _COMMENT_PROBE))


def _algebra_of(sparql: str) -> CompValue | None:
    try:
        return prepareQuery(sparql).algebra
    except Exception:
        return None


def _shape(value: Any) -> Any:
    """A comparable form of an algebra value that keeps every node's name (CompValue equality ignores it)."""
    if isinstance(value, CompValue):
        return value.name, tuple((key, _shape(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(_shape(item) for item in value)
    return value


def _core(algebra: CompValue) -> tuple:
    """The query's structure apart from what the rewrites may change: the dataset clause and the top LIMIT."""
    top = _field(algebra, "p")
    if isinstance(top, CompValue) and top.name == "Slice":
        pattern = (int(_field(top, "start") or 0), _shape(_field(top, "p")))
    else:
        pattern = (0, _shape(top))
    rest = tuple((key, _shape(item)) for key, item in algebra.items() if key not in ("datasetClause", "p"))
    return algebra.name, rest, pattern


def _depths(masked: str) -> list[int]:
    """Brace depth at each offset (depth of the character itself)."""
    depth, depths = 0, []
    for ch in masked:
        if ch == "}":
            depth -= 1
        depths.append(depth)
        if ch == "{":
            depth += 1
    return depths


def _paren_free(masked: str) -> list[bool]:
    """Whether each offset is outside every parenthesis."""
    depth, outside = 0, []
    for ch in masked:
        if ch == ")":
            depth -= 1
        outside.append(depth <= 0)
        if ch == "(":
            depth += 1
    return outside


def _matching_brace(masked: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(masked)):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Unbalanced braces in SPARQL")


def _field(node: CompValue | None, key: str):
    """``node[key]``, or ``None`` when absent.

    Not ``CompValue.get``: for a missing key it falls back to variable lookup and
    returns the key NAME, so ``node.get("length")`` on an unbounded query is the
    truthy string ``"length"``.
    """
    if node is None or key not in node:
        return None
    return node[key]


def _top_limit(algebra: CompValue) -> int | None:
    top = _field(algebra, "p")
    if top is None or top.name != "Slice":
        return None
    length = _field(top, "length")
    return None if length is None else int(length)


def _attr_views(node: CompValue) -> list[Any]:
    """Values rdflib keeps as instance attributes rather than dict items (translateExists sets ``graph``)."""
    return [v for k, v in vars(node).items() if k != "name" and isinstance(v, (CompValue, list, tuple, ParseResults))]


def _children(node: Any) -> list[Any]:
    """What a walk visits under ``node``: its items, and for a CompValue its attribute views too."""
    if isinstance(node, CompValue):
        return [node[key] for key in list(node.keys())] + _attr_views(node)
    if isinstance(node, (list, tuple, set, ParseResults)):
        return list(node)
    return []


def _once(root: Any, children: Callable[[Any], list[Any]]) -> Iterator[Any]:
    """Every node under ``root``, each object once.

    rdflib keeps one subtree both as an item and as an attribute, at every level of nesting, so a
    walk that follows both without remembering what it saw takes time exponential in the depth.
    """
    seen: set[int] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        stack.extend(reversed(children(node)))


def _walk(node: Any) -> Iterator[CompValue]:
    return (n for n in _once(node, _children) if isinstance(n, CompValue))


def _tree_of(sparql: str) -> ParseResults:
    """The parse tree: unlike the algebra, it keeps every pattern the text has."""
    try:
        return parseQuery(sparql)
    except Exception as e:
        raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Query does not parse as a tree") from e


def _iris(node: Any) -> Iterator[URIRef]:
    """Every IRI in an algebra, its prefixed names expanded."""
    return (n for n in _once(node, _children) if isinstance(n, URIRef))


def _names_a_hint(text: str, algebra: CompValue) -> bool:
    hints = NEPTUNE_HINTS.lower()
    return bool(_HINT_TEXT_RE.search(text)) or any(str(iri).lower().startswith(hints) for iri in _iris(algebra))


def _timeout_hint(timeout_ms: int) -> str:
    return f"<{NEPTUNE_HINTS}Query> <{NEPTUNE_HINTS}queryTimeout> {int(timeout_ms)} ."


def _count(nodes: list[CompValue], names: tuple[str, ...]) -> int:
    return sum(node.name in names for node in nodes)


def _is_the_graph(term: Any, named_graph: str) -> bool:
    # A variable, or a prefixed name the parse tree has not expanded, is never the graph.
    return isinstance(term, URIRef) and str(term) == named_graph


class SPARQLFirewall:
    """Applies F1–F5 to a generated SPARQL query and returns the text to execute."""

    def __init__(self, *, default_max_results: int = DEFAULT_MAX_RESULTS):
        """Configure the result cap used when the caller asks for none."""
        self._default_max_results = default_max_results

    def check(
        self,
        sparql: str,
        *,
        named_graph: str | None = None,
        max_results: int | None = None,
        query_timeout_ms: int | None = None,
        denied_predicates: frozenset[str] = frozenset(),
    ) -> SafeQuery:
        """Gate one query.

        Args:
            sparql: The generated query.
            named_graph: The source's ``configuration.dataNamedGraph``, or ``None``
                when its A-Box is the endpoint's default graph.
            max_results: Caller's requested row cap; never raises the default cap.
            query_timeout_ms: The server-side timeout the query must carry (F6), for an
                engine that takes one; ``None`` sends none.
            denied_predicates: Predicate IRIs the caller's grant denies on this source
                (exact strings). With any, F7 refuses a query that names one or that
                could read an unnamed predicate (a variable predicate, a negated set).

        Returns:
            The query to execute, scoped and bounded.

        Raises:
            UnsafeSPARQLError: Any rule failed; nothing may be executed.
        """
        if named_graph is not None and not NAMED_GRAPH_IRI_RE.fullmatch(named_graph):
            # Validated at registration too; re-checked at the point of interpolation.
            raise UnsafeSPARQLError(FirewallRule.GRAPH_SCOPE, "Source named graph is not a safe IRI")
        cap = self._cap(max_results)

        text = (sparql or "").strip().rstrip(";").rstrip()
        if _ESCAPE_RE.search(text):
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Codepoint escapes (\\u, \\U) are not allowed")
        algebra = self._parse(text)  # F1 / F4: the text as submitted must parse completely
        text = self._uncommented(text, algebra)  # F4: comment boundaries are the parser's
        form = QueryForm(algebra.name)
        if _names_a_hint(text, algebra):
            raise UnsafeSPARQLError(FirewallRule.WORK_BOUND, "Engine query hints are not allowed")
        _refuse_escapes_outside_literals(text)  # F4: names must be compared as the engine reads them
        _refuse_relative_iris(text)  # F4: so must IRIs
        _refuse_repeated_prefix_labels(text)  # F4: and prefixed names
        self._check_patterns(text, algebra, named_graph, submitted=True)  # F5, F2, F4
        if form == QueryForm.DESCRIBE:
            raise UnsafeSPARQLError(
                FirewallRule.RESULT_BOUND, "DESCRIBE is not allowed: a LIMIT cannot bound the triples it returns"
            )

        if named_graph is not None:
            text = self._scope_to_graph(text, form, named_graph)
        limit: int | None = None
        # ASK returns one boolean: no solution sequence to bound.
        if form != QueryForm.ASK:
            text, limit = self._bound(text, algebra, cap)  # F3

        self._verify(text, form, named_graph, limit, algebra)  # F4: every rule again, on the text executed
        if denied_predicates:
            _check_denied_predicates(algebra, denied_predicates)  # F7, on the algebra
            # and on the parse tree, which keeps the patterns rdflib's translation can lose
            _check_denied_predicates(_expanded_tree(_tree_of(text)), denied_predicates)
        if query_timeout_ms is not None:
            text = self._with_timeout(text, form, named_graph, limit, query_timeout_ms)  # F6
        variables = tuple(str(v) for v in _field(algebra, "PV") or []) if form == QueryForm.SELECT else ()
        return SafeQuery(sparql=text, form=form, limit=limit, variables=variables)

    def _cap(self, max_results: int | None) -> int:
        if max_results is None or max_results <= 0:
            return self._default_max_results
        return min(int(max_results), self._default_max_results)

    @staticmethod
    def _parse(sparql: str) -> CompValue:
        if not sparql or not sparql.strip():
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Empty SPARQL")
        try:
            algebra = prepareQuery(sparql).algebra
        except Exception as e:
            # Every update form lands here: the query grammar cannot parse it.
            raise UnsafeSPARQLError(FirewallRule.READ_ONLY, f"Not a read-only SPARQL query ({type(e).__name__})") from e
        if algebra.name not in {f.value for f in QueryForm}:
            raise UnsafeSPARQLError(FirewallRule.READ_ONLY, f"Unsupported query form {algebra.name}")
        return algebra

    @classmethod
    def _check_patterns(
        cls, text: str, algebra: CompValue, named_graph: str | None, *, submitted: bool = False
    ) -> None:
        """F5, F2 and F4 over the algebra, the parse tree and the text; any one view refuses.

        ``submitted``: the caller's text, which may carry no dataset clause (the rewrite adds the only one).
        """
        walked = list(_walk(algebra))
        tree = list(_walk(_tree_of(text)))
        masked = _blank_names(_mask(text))
        # F5 first, so a SERVICE is always reported as F5, even in a query F2 would also reject.
        cls._check_no_federation([*walked, *tree], masked)
        if submitted and _field(algebra, "datasetClause"):
            raise UnsafeSPARQLError(FirewallRule.GRAPH_SCOPE, "FROM / FROM NAMED is not allowed")
        cls._check_graph_blocks([*walked, *tree], text, masked, named_graph)
        cls._check_views_agree(walked, tree, masked)

    @staticmethod
    def _check_no_federation(nodes: list[CompValue], masked: str) -> None:
        if any(node.name == "ServiceGraphPattern" for node in nodes) or _SERVICE_RE.search(masked):
            raise UnsafeSPARQLError(FirewallRule.FEDERATED, "SERVICE (federated query) is not allowed")

    @staticmethod
    def _check_graph_blocks(nodes: list[CompValue], text: str, masked: str, named_graph: str | None) -> None:
        # Inside EXISTS / NOT EXISTS rdflib keeps the parse-tree GraphGraphPattern, not the algebra Graph.
        terms = [_field(node, "term") for node in nodes if node.name in ("Graph", "GraphGraphPattern")]
        keywords = list(_GRAPH_RE.finditer(masked))
        if named_graph is None:
            if terms or keywords:
                raise UnsafeSPARQLError(
                    FirewallRule.GRAPH_SCOPE, "GRAPH is not allowed: this source reads its default graph"
                )
            return
        named_iri = re.compile(rf"\s*<{re.escape(named_graph)}>")
        if not all(_is_the_graph(term, named_graph) for term in terms) or not all(
            named_iri.match(text, m.end()) for m in keywords
        ):
            raise UnsafeSPARQLError(FirewallRule.GRAPH_SCOPE, "GRAPH may only name this source's graph")

    @staticmethod
    def _check_views_agree(walked: list[CompValue], tree: list[CompValue], masked: str) -> None:
        """Fail closed when the text, the parse tree and the algebra see different graph patterns."""
        for word, (keyword, names) in _PATTERN_NODES.items():
            if len(keyword.findall(masked)) != _count(tree, names):
                raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, f"The parser and the text disagree on {word}")
        for names in (("GraphGraphPattern",), ("ServiceGraphPattern",)):
            if _count(walked, ("Graph", *names)) < _count(tree, names):
                raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "The algebra lacks a pattern the query has")

    @staticmethod
    def _scope_to_graph(text: str, form: QueryForm, named_graph: str) -> str:
        """Insert ``FROM <g> FROM NAMED <g>`` where the grammar puts the dataset clause.

        That is after the form's head (and, for CONSTRUCT, its template) and before
        the WHERE clause. DESCRIBE, the one form whose WHERE clause is optional, is
        refused before scoping, so a query without one fails closed.
        """
        masked = _mask(text)
        head = _FORM_RE.search(masked)
        if head is None:
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Query form keyword not found")
        pos = head.end()
        if form == QueryForm.CONSTRUCT:
            after = masked[pos:].lstrip()
            if after.startswith("{"):
                pos = _matching_brace(masked, masked.index("{", pos)) + 1
        # Outside parentheses: a brace or WHERE in the projection (an EXISTS expression) is not the WHERE clause.
        outside = _paren_free(masked)
        where = next((m.start() for m in _WHERE_RE.finditer(masked, pos) if outside[m.start()]), None)
        brace = next((i for i in range(pos, len(masked)) if masked[i] == "{" and outside[i]), None)
        candidates = [at for at in (where, brace) if at is not None]
        if not candidates:
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "WHERE clause not found")
        at = min(candidates)
        clause = f"FROM <{named_graph}> FROM NAMED <{named_graph}>"
        return f"{text[:at].rstrip()}\n{clause}\n{text[at:].lstrip()}".rstrip()

    @classmethod
    def _with_timeout(
        cls, text: str, form: QueryForm, named_graph: str | None, limit: int | None, timeout_ms: int
    ) -> str:
        """Open the WHERE group with the timeout hint, then check that only the hint was added."""
        masked = _mask(text)
        head = _FORM_RE.search(masked)
        if head is None:
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Query form keyword not found")
        pos = head.end()
        if form == QueryForm.CONSTRUCT:
            if not masked[pos:].lstrip().startswith("{"):
                raise UnsafeSPARQLError(
                    FirewallRule.WORK_BOUND, "CONSTRUCT WHERE cannot carry the server timeout; write its template"
                )
            pos = _matching_brace(masked, masked.index("{", pos)) + 1
        outside = _paren_free(masked)
        brace = next((i for i in range(pos, len(masked)) if masked[i] == "{" and outside[i]), None)
        if brace is None:
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "WHERE group not found")
        hint = _timeout_hint(timeout_ms)
        hinted = f"{text[: brace + 1]} {hint} {text[brace + 1 :]}"
        algebra = _algebra_of(hinted)
        if algebra is None or algebra.name != form.value:
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Query with the timeout does not parse")
        cls._check_patterns(hinted, algebra, named_graph)
        cls._check_scope(algebra, named_graph)
        cls._check_bound(algebra, limit)
        if _parser_reads_a_comment(hinted):
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Query with the timeout has a comment")
        return hinted

    @staticmethod
    def _bound(text: str, algebra: CompValue, cap: int) -> tuple[str, int]:
        """Ensure a top-level LIMIT no larger than ``cap``."""
        existing = _top_limit(algebra)
        masked = _mask(text)
        depths = _depths(masked)
        if existing is not None:
            if existing <= cap:
                return text, int(existing)
            top_level = [m for m in _LIMIT_RE.finditer(masked) if depths[m.start()] == 0]
            if not top_level:
                raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Top-level LIMIT not found")
            m = top_level[-1]
            return f"{text[: m.start(1)]}{cap}{text[m.end(1) :]}", cap
        # A trailing top-level VALUES clause must stay last; insert before it.
        trailing_values = [m for m in _VALUES_RE.finditer(masked) if depths[m.start()] == 0]
        if trailing_values:
            at = trailing_values[-1].start()
            return f"{text[:at].rstrip()}\nLIMIT {cap}\n{text[at:]}", cap
        # Comments are already cut (_uncommented), so appending is safe; the newline is for readability.
        return f"{text}\nLIMIT {cap}", cap

    @staticmethod
    def _uncommented(text: str, algebra: CompValue) -> str:
        """``text`` without comments, where the masker and the parser agree on them; else fail closed."""
        stripped = _without_comments(text)
        if stripped != text and _algebra_of(stripped) != algebra:
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Comment boundaries disagree with the parser")
        if _parser_reads_a_comment(stripped):
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "A comment the rewrites cannot see")
        return stripped

    @classmethod
    def _verify(
        cls, text: str, form: QueryForm, named_graph: str | None, limit: int | None, checked: CompValue | None = None
    ) -> None:
        """F4 on the final text, the one executed: every rule again, and the checked query plus only the rewrites."""
        algebra = _algebra_of(text)
        if algebra is None:
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Rewritten query does not parse")
        if algebra.name != form.value:
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Rewritten query changed form")
        _refuse_escapes_outside_literals(text)
        _refuse_relative_iris(text)
        cls._check_patterns(text, algebra, named_graph)  # F5, F2, F4
        cls._check_scope(algebra, named_graph)
        cls._check_bound(algebra, limit)  # F3
        if checked is not None and _core(algebra) != _core(checked):
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Rewritten query differs from the checked query")
        if _parser_reads_a_comment(text):
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Rewritten query has a comment")

    @staticmethod
    def _check_scope(algebra: CompValue, named_graph: str | None) -> None:
        dataset = set()
        for clause in _field(algebra, "datasetClause") or []:
            kind = "default" if _field(clause, "default") is not None else "named"
            dataset.add((kind, str(_field(clause, kind))))
        expected = {("default", named_graph), ("named", named_graph)} if named_graph is not None else set()
        if dataset != expected:
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Rewritten query is not scoped as intended")

    @staticmethod
    def _check_bound(algebra: CompValue, limit: int | None) -> None:
        if limit is None:
            return
        length = _top_limit(algebra)
        if length is None or length > limit:
            raise UnsafeSPARQLError(FirewallRule.RESULT_BOUND, "Rewritten query is not bounded")


def _refuse_escapes_outside_literals(text: str) -> None:
    r"""Refuse a backslash anywhere but inside a string literal.

    rdflib keeps a local-name escape (``ex:\\#title``) as written, while an engine following the
    grammar reads the IRI it spells, so the IRIs the checks compare would not be the ones run.
    Generated queries have no need for them.
    """
    if "\\" in _mask(text):
        raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Backslash escapes outside string literals are not allowed")


_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_DOT_SEGMENT_RE = re.compile(r"/\.\.?(?:/|#|\?|$)")


def _refuse_relative_iris(text: str) -> None:
    """Refuse BASE, IRIs without a scheme and IRIs with dot segments, read from the parse tree.

    A relative IRI resolves against a base the checks never compare, and an engine that removes
    dot segments reads ``x/../o#title`` as ``o#title``. Read from the parse tree, so words such as
    ``base:name`` and comparisons such as ``?n<5&&?m>3`` are not mistaken for either.
    """
    tree = _tree_of(text)
    prologue = tree[0] if len(tree) else []
    if any(isinstance(decl, CompValue) and decl.name == "Base" for decl in prologue):
        raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "BASE is not allowed")
    for iri in _iris(tree):
        if not _SCHEME_RE.match(iri):
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "Relative IRIs are not allowed")
    for iri in _iris(_expanded_tree(tree)):
        if _DOT_SEGMENT_RE.search(iri):
            raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "IRIs with dot segments are not allowed")


def _refuse_repeated_prefix_labels(text: str) -> None:
    """Refuse a prologue that declares one prefix label twice.

    rdflib keeps the last declaration and an engine may keep the first, so the checks and the
    engine could expand ``ex:title`` to different IRIs.
    """
    tree = _tree_of(text)
    labels = [decl.get("prefix") or "" for decl in (tree[0] if len(tree) else []) if decl.name == "PrefixDecl"]
    if len(labels) != len(set(labels)):
        raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, "A prefix label declared twice is not allowed")


def _expanded_tree(tree: ParseResults) -> Any:
    """The query part of a parse tree with its prefixed names expanded to IRIs (as rdflib's translation does)."""
    prologue = translatePrologue(tree[0], None)
    return traverse(tree[1], visitPost=functools.partial(translatePName, prologue=prologue))


def _check_denied_predicates(algebra: CompValue, denied: frozenset[str]) -> None:
    """F7: refuse a query that names a denied predicate anywhere, or could read one it does not name.

    Every IRI the parsed query holds is compared, exactly, so a prefixed name, a full IRI, a path
    step or a FILTER EXISTS pattern all count. A variable predicate or a negated property set
    could match a denied predicate without naming it, so either refuses while any is denied.
    """
    for node in _once(algebra, _grant_children):
        if isinstance(node, URIRef) and str(node) in denied:
            raise UnsafeSPARQLError(FirewallRule.GRANT, "The query reads a predicate the caller's grant denies")
        if isinstance(node, NegatedPath) or (isinstance(node, CompValue) and node.name == "PathNegatedPropertySet"):
            raise UnsafeSPARQLError(FirewallRule.GRANT, "A negated property set may read a denied predicate")


def _grant_children(node: Any) -> list[Any]:
    """What F7 walks next under ``node``; a variable predicate in a triple pattern refuses."""
    if isinstance(node, InvPath):
        return [node.arg]
    if isinstance(node, MulPath):
        return [node.path]
    if isinstance(node, (SequencePath, AlternativePath)):
        return list(node.args)
    if isinstance(node, (CompValue, dict)):
        # A triples entry is one or more (subject, predicate, object) runs: a ';' or ',' list inside
        # EXISTS arrives flattened into one entry, so every third term is a predicate.
        if any(isinstance(p, Variable) for triple in _field(node, "triples") or [] for p in list(triple)[1::3]):
            raise UnsafeSPARQLError(FirewallRule.GRANT, "A variable predicate may read a denied predicate")
        values = [value for key, value in node.items() if key != "_vars"]
        return values + (_attr_views(node) if isinstance(node, CompValue) else [])
    if isinstance(node, (list, tuple, set, frozenset, ParseResults)):
        return list(node)
    if node is None or isinstance(node, _GRANT_LEAVES):
        return []
    # A node of a shape the walk doesn't know could hide a pattern: fail closed.
    raise UnsafeSPARQLError(FirewallRule.FAIL_CLOSED, f"Unrecognised query node: {type(node).__name__}")


_GRANT_LEAVES = (URIRef, Variable, Literal, BNode, str, int, float, Decimal)
