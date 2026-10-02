# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Parser for the ``@pk`` / ``@fk(...)`` constraint tags carried in column comments.

The Athena federation protocol has **no** field for key constraints anywhere in
its metadata path, so a connector cannot report declared primary/foreign keys the
way the JDBC path does (which reads them from ``information_schema``). To reach
parity, declared keys travel **inside the column comments** the connector already
emits, as tags that discovery parses out, strips, and turns into the same
``PrimaryKey``/``ForeignKey`` records — ``DETERMINISTIC``, confidence 1.0 — that
``jdbc.py::_safe_constraints`` produces.

Grammar (case-sensitive; one comment may carry human text plus one or more tags)::

    comment   := (human_text | tag)*
    tag       := "@pk" | "@notnull" | "@fk" "(" reference ")"
    reference := segment ("." segment)*        ; the last two are TABLE.COLUMN
    segment   := pad (quoted | unquoted) pad
    quoted    := '"' (not_a_quote | '""')* '"' ; may contain "." and ")"
    unquoted  := [A-Za-z0-9_$-]+               ; no ".", no ")", no whitespace
    pad       := whitespace*

Semantics:
  * ``@pk`` marks the column as a member of the table's primary key. A composite
    PK is the set of ``@pk`` columns in ``DESCRIBE`` order → one ``PrimaryKey``.
  * ``@notnull`` marks the column as non-nullable. Operand-free, like ``@pk``, and it
    tags the EXCEPTION rather than the rule: absence must keep meaning *unknown*,
    because every connector deployed to date emits no such tag. A ``@nullable``
    spelling would instead reinterpret every already-deployed connector's columns.
    Read only by the review UI's Nullable column; induction reads only a declared
    primary key for ``NOT_NULL``.
  * ``@fk(orders.customer_id)`` on the child column → one single-column
    ``ForeignKey``. Quote the segments that need it, and only those:
    ``@fk("my orders"."customer id")``.
  * A **composite** FK is expressed as one ``@fk(...)`` per participating child
    column, and stored as N single-column ``ForeignKey`` rows — exactly as the
    JDBC path already flattens composite FKs. There is deliberately no
    ordinal/group sub-grammar: ``ForeignKey`` has no constraint-group field, so a
    group id would produce byte-identical stored rows.
  * Understood tags are stripped; the remaining human text is the column
    description.
  * A tag the parser cannot act on is logged and ignored. Parsing is never
    fatal — a bad tag costs one constraint, not the scan.

Resolved grammar details (the format spec shipped to connector authors must
match these, since the encoder helper generates against them):

  * **One spelling, and the operand is bracketed.** ``@fk(orders.order_id)`` is
    the only ``@fk`` form. The bracket carries the whole design: it is the tag's
    **terminator**, so the parser never has to guess where a reference stops from
    the characters that follow it. That single change is what removes the two
    limitations the character-run form had — prose bound directly to the
    reference, and a trailing sentence dot — and what makes quoting a free choice
    per segment instead of an all-or-nothing commitment (see *Resolved
    limitations* below).
  * **Quoting is per segment, optional, and independent** — exactly as SQL
    allows. ``@fk("db"."orders".order_id)``, ``@fk("t".col)`` and
    ``@fk(t."c")`` are all legal and all mean what they read as. Quote a segment
    only when the name needs it (a space, a dot, a bracket, a quote, or anything
    outside ``[A-Za-z0-9_$-]``). A segment must be *entirely* quoted or entirely
    bare, though: ``@fk("a"b.c)`` is an author error rather than the table
    ``ab``, because silently concatenating the two halves would store a name
    nobody wrote.
  * **The quote escape.** A quoted segment is delimited by ``"`` and a literal
    double quote inside it is written doubled (``""``), per SQL — so
    ``@fk("say ""hi"" now".x)`` targets the table ``say "hi" now``. Segment text
    is stored decoded: quotes removed, ``""`` collapsed to ``"``.
  * **The child column is never named, so it never needs quoting.** The tag sits
    in that column's own comment, so discovery takes the child name from
    ``DESCRIBE``; only the parent ``TABLE.COLUMN`` is spelled in the tag.
  * **Delimiting.** The reference is everything between ``@fk(`` and the first
    ``)`` that is **not inside a quoted segment**; that bracket ends the tag.
    Two consequences worth stating for an encoder: a name containing ``)`` is
    expressible as long as it is quoted (``@fk("total (usd)".x)``), and prose may
    follow the tag **immediately** — ``@fk(orders.order_id)See notes`` and
    ``@fk(orders.order_id).`` both parse, with the text after the bracket left in
    the description untouched.
  * **Whitespace inside the brackets is padding** and is dropped, so
    ``@fk( orders . order_id )`` is the same tag as ``@fk(orders.order_id)``.
    Whitespace *within* an unquoted segment is not padding and not an identifier
    character: ``@fk(orders.order id)`` is malformed, and so — usefully — is
    ``@fk(orders.order_id, nullable)``, where prose was left inside the brackets.
    Both are reported rather than stored under a name nobody meant.
  * **Boundaries.** A tag must not be preceded by an identifier character, so an
    address or handle inside prose (``"owner bob@pk.example.com"``,
    ``'ask bob@fk(orders.id)'``) is not a tag.
  * **Extra leading qualification is tolerated.** The last two segments are
    ``TABLE.COLUMN``; anything before them is catalog/database/schema and is
    dropped, so ``@fk(db.public.orders.order_id)`` and ``@fk(orders.order_id)``
    resolve alike.
  * **Position.** Tags are recognised anywhere in the comment, not only in the
    trailing run the grammar draws. Restricting them to the tail would silently
    drop the keys in ``"@pk Customer identifier"`` for no benefit.
  * **Whitespace around a tag.** Only the whitespace directly preceding a tag is consumed, and
    the result is trimmed — so a tag-only comment cleans to ``""`` (not a blank
    string), while whitespace *inside* human text is preserved verbatim. Text
    that abuts the closing bracket therefore abuts the text before the tag:
    ``"Parent @fk(orders.id)See notes"`` cleans to ``"ParentSee notes"``.
  * **Case, and other near misses.** Recognition is case-sensitive, per the
    grammar. ``@PK``, ``@fk`` with no operand, ``@pk=customer_id``,
    ``@pk(customer_id)`` (``@pk`` takes no operand, bracketed or otherwise),
    ``@notnull=true`` and ``@notnull(x)`` are near misses, not tags. So is
    ``@fk=orders.order_id``: the ``@fk=`` spelling
    was **retired** in favour of the bracketed operand, and because it is a
    plausible thing for an author to write from memory it is diagnosed
    (``fk_equals_spelling_retired``) rather than read as prose.
  * **Nothing unrecognised is removed.** A tag is stripped if and only if the
    parser acted on it. Everything else — a near miss, and equally an ``@fk(...)``
    whose reference is unusable (``@fk(orders)``, ``@fk(orders.)``, ``@fk()``,
    ``@fk(orders.id`` with no closing bracket, ``@fk("orders.id)`` with no
    closing quote) — is logged with a specific reason and **left in the
    description verbatim**. The two are the same class of author mistake and get
    the same treatment: the parser never deletes text it did not understand, and
    because these warnings land in Orion's logs rather than the connector
    author's, the stored description is the only feedback channel the author can
    actually observe. A leftover ``@fk(orders)`` in a catalog description is the
    signal that gets the tag fixed.
  * **References are matched exactly.** A reference is stored as the DECODED
    identifier and resolved downstream by exact name match, so it must be spelled
    as ``DESCRIBE`` reports the target (lower case, for Athena). Quoting affects
    only how the name is written in the tag, never what is stored:
    ``@fk("orders"."Order Date")`` stores the column as ``Order Date``. A case or
    spelling mismatch does not error — it reads as a target outside the scan.
  * **Duplicate references dedup by target, not by spelling.** Because only the
    decoded pair is stored, ``@fk(orders.id)`` and ``@fk("orders"."id")`` on one
    column are one key, not two. Any other rule would store two byte-identical
    ``ForeignKey`` rows.

Resolved limitations (recorded because the previous spelling documented them as
unavoidable, and an encoder written against that spelling would still be
avoiding them):

  * **Prose adjacency is no longer a limitation.** With a bare character run,
    ``@fk=orders.order_id.See notes`` resolved to ``order_id.See``, and that was
    genuinely undetectable because it is the same shape as the legitimate
    qualification in ``@fk=public.orders.order_id``. The closing bracket now ends
    the tag, so the dot and the word after it are prose by construction, and the
    "separate a tag from following prose with whitespace" rule is gone.
  * **A trailing sentence dot needs no tolerance.** ``@fk(orders.order_id).``
    parses without the old ``rstrip(".")`` fudge, so ``@fk(orders.)`` — which
    that fudge had to accept as a usable reference — is now reported as the empty
    column name it is.
  * **Mixed quoting is no longer silently mis-targeted.** Under the
    all-or-nothing rule ``@fk="db"."orders".order_id`` ended after ``"orders"``
    and produced a usable-looking key to ``db.orders`` with ``.order_id`` left as
    prose, indistinguishable from a complete quoted reference followed by prose.
    Per-segment quoting inside a bracket makes it exactly what it reads as,
    ``orders.order_id``.

One ambiguity survives, and it follows from the qualification rule rather than
from the delimiters: **a dotted bare word inside the brackets reads as
qualification.** ``@fk(orders.order_id.see)`` resolves to ``order_id.see``,
because nothing distinguishes it from ``@fk(db.orders.order_id)`` — the rule that
tolerates extra leading qualification is the same rule that accepts a third
segment here. It now takes prose written *inside* the operand to hit, and the
common shapes of that mistake (a space, a comma, a closing bracket) are rejected
outright; only a single unbroken word attached by a dot gets through.

Note on the examples above: no example here can show a name whose **last**
character is a quote, because the escaped quote plus the segment's own closing
quote form a three-quote run that would end this docstring early — which is why
the escape is illustrated with the quotes in the middle of the name
(``@fk("say ""hi"" now".x)``). The restriction is the docstring's, not the
grammar's: a table named ``he said "hi"`` is perfectly expressible, and that
shape is pinned in the tests instead.

``_split_reference`` mirrors ``parse_referred_column`` in the ontology engine's
``inducer/services/data_catalog.py`` — same tolerance for extra leading
qualification, same "last two segments are ``TABLE.COLUMN``" rule. It is mirrored
rather than imported because ``coa-sources`` does not depend on ``coa-ontology``;
behaviour must stay in step by hand. The mirror covers that rule only: quoting
and bracketing are properties of the tag syntax, and the catalog references that
splitter reads never carry either.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from coa_common.domain_models import EnrichmentSource, ForeignKey, PrimaryKey

logger = logging.getLogger(__name__)

# Characters a BARE (unquoted) segment may contain. A deliberate SUPERSET of
# Athena's unquoted-identifier set ([A-Za-z0-9_]): "-" is legal in Glue
# database/table names (which Athena reaches by backtick-quoting) and "$" appears
# in Hive-style names. It excludes "." (the segment separator), ")" (the tag
# terminator), '"' (the quote), and whitespace — each of which has to be spelled
# inside a quoted segment instead. Unlike the pre-bracket grammar's character
# class, this one does not decide where a tag ENDS (the bracket does), so it is a
# validator: a segment it rejects is a malformed tag, reported and left in place,
# rather than a reference silently cut short.
_UNQUOTED_SEGMENT = re.compile(r"[A-Za-z0-9_$\-]+")

# Finds the HEAD of every tag-shaped token — "@" plus a pk/fk-ish name — in one
# pass, so each is classified exactly once (valid, malformed, or unrecognised) and
# only an understood tag is removed. What follows the head (an operand, the retired
# "=", or nothing) is read by hand rather than folded into this pattern. A regex
# CAN describe a well-formed operand, but it cannot serve the other two things this
# parser owes the author: the tag's extent reaches past the head match, which
# re.sub has no way to replace, and a failed match reports only that it failed,
# where one quote-aware walk also names WHICH delimiter was left open.
# "\s*" swallows the whitespace preceding a tag so that removing it leaves no seam
# in the human text ("a @pk b" → "a b"). The lookbehind demands a left boundary:
# without it an address in prose ("owner bob@pk.example.com") reads as a "@pk"
# tag, silently declaring a primary key nobody asked for and corrupting the
# description. The name group is greedy, so "@pkey"/"@pk_id"/"@fkeys"/"@notnulls"
# land in the group as typos to report rather than matching a tag; the
# case-insensitive name lets wrong-case near misses ("@PK", "@NOTNULL") be recognised
# as mistakes instead of read as prose.
#
# "notnull" is spelled out in the alternation rather than reached by a looser name class,
# so a word that merely starts the same way ("@note") stays prose as it was before.
_TAG_HEAD = re.compile(r"\s*(?<![A-Za-z0-9_$])@(?P<name>(?i:pk|fk|notnull)[A-Za-z0-9_]*)")


@dataclass(frozen=True)
class ParsedFk:
    """One ``@fk(...)`` target parsed off a column comment."""

    target_table: str
    target_column: str


@dataclass(frozen=True)
class ParsedComment:
    """The result of parsing one column comment.

    Attributes:
        description: The comment with the understood tags stripped and the result
            trimmed — store this as ``business_metadata.description``. Empty when
            the comment held nothing but tags. A tag the parser could not act on
            is left here verbatim, so the author can see the mistake.
        is_pk_member: Whether the column carried ``@pk``.
        is_not_null: Whether the column carried ``@notnull``. ``False`` also means
            "nobody said", and is treated as nullable downstream.
        foreign_keys: Valid ``@fk(...)`` targets in the order they appeared,
            duplicates removed (a repeated tag cannot mean two keys). Dedup
            compares the DECODED ``(table, column)`` pair, not the tag text, so
            ``@fk(orders.id)`` and ``@fk("orders"."id")`` on one column collapse
            to a single key — they name the same target, and two identical
            ``ForeignKey`` rows would be indistinguishable downstream anyway.
    """

    description: str
    is_pk_member: bool
    foreign_keys: tuple[ParsedFk, ...]
    # Defaulted, not positional, so existing construction sites keep their meaning.
    is_not_null: bool = False


@dataclass(frozen=True)
class _Operand:
    """The bracketed operand of one ``@fk`` tag, as read off the comment.

    Attributes:
        segments: The RAW ``.``-separated segments — quotes still in place,
            padding not yet dropped. Empty when the operand could not be read at
            all; a successful scan always yields at least one segment.
        end: Index just past the terminating ``)``. On failure there is no
            terminator, so this is the end of the comment: the scan looked that
            far for one.
        failure: Why the operand could not be read, or ``None`` on success. This
            is the field to dispatch on.
    """

    segments: tuple[str, ...]
    end: int
    failure: str | None


def parse_comment(raw: str | None) -> ParsedComment:
    """Parse constraint tags out of one column comment.

    Args:
        raw: The comment as returned by ``DESCRIBE`` (``None`` when absent).

    Returns:
        The cleaned description plus the tags found. A tag the parser cannot act
        on is logged and left in the text; this function never raises on bad
        input.
    """
    if not raw:
        return ParsedComment(description="", is_pk_member=False, foreign_keys=())

    # Bound to a name of non-optional type so the readers below close over a str.
    comment: str = raw
    foreign_keys: list[ParsedFk] = []
    is_pk_member = False
    is_not_null = False

    def _leave_in_place(head: re.Match[str], event: str, reason: str, *, tag_end: int, resume: int) -> tuple[int, str]:
        """Report a tag-shaped token the parser could not act on.

        Args:
            head: The ``_TAG_HEAD`` match for the token.
            event: The warning's event name — ``constraint_tag_malformed`` for a
                tag whose operand could not be used, ``constraint_tag_unrecognised``
                for a near miss that is not this grammar's tag at all.
            reason: The stable reason string for the operator log.
            tag_end: End of the span to quote in the log. Wider than ``resume``
                when the tag has no discoverable end (an unclosed bracket or
                quote), where the useful thing to show is how far the scan looked.
            resume: Where scanning continues. Never past the token's own head for
                an unterminated tag, so that a later, well-formed tag in the same
                comment is still parsed.

        Returns:
            ``(resume index, replacement text)`` — the replacement is the skipped
            span verbatim, since nothing the parser did not understand is removed.
        """
        logger.warning(event, extra={"tag": comment[head.start() : tag_end].strip(), "reason": reason})
        return resume, comment[head.start() : resume]

    def _read_tag(head: re.Match[str]) -> tuple[int, str]:
        """Classify one tag-shaped token and act on it.

        Args:
            head: The ``_TAG_HEAD`` match for the token.

        Returns:
            ``(index to continue scanning from, text that replaces the span from
            the head's start to that index)``. The replacement is ``""`` for a
            tag the parser acted on and the span verbatim for anything else.
        """
        nonlocal is_pk_member, is_not_null
        name = head.group("name")
        # "=" and "(" are the only characters that turn a bare name into a
        # near-miss rather than a tag, so one character of lookahead classifies.
        marker = comment[head.end() : head.end() + 1]

        if name == "pk":
            if marker in ("=", "("):
                return _leave_in_place(
                    head,
                    "constraint_tag_unrecognised",
                    "pk_takes_no_operand",
                    tag_end=head.end() + 1,
                    resume=head.end(),
                )
            is_pk_member = True
            return head.end(), ""

        # The "@pk" arm, character for character, including its refusal of the "=" and
        # "(" markers: that refusal is what leaves "@notnull=true" and "@notnull(x)" in
        # the description as literal text, which is the author's only feedback channel.
        if name == "notnull":
            if marker in ("=", "("):
                return _leave_in_place(
                    head,
                    "constraint_tag_unrecognised",
                    "notnull_takes_no_operand",
                    tag_end=head.end() + 1,
                    resume=head.end(),
                )
            is_not_null = True
            return head.end(), ""

        if name == "fk":
            if marker == "=":
                return _leave_in_place(
                    head,
                    "constraint_tag_unrecognised",
                    "fk_equals_spelling_retired",
                    tag_end=head.end() + 1,
                    resume=head.end(),
                )
            if marker != "(":
                return _leave_in_place(
                    head,
                    "constraint_tag_unrecognised",
                    "fk_requires_a_bracketed_operand",
                    tag_end=head.end(),
                    resume=head.end(),
                )
            operand = _scan_operand(comment, head.end())
            if operand.failure is not None:
                # No terminator was found, so the tag has no known extent: report
                # how far the scan looked, but resume right after the head.
                return _leave_in_place(
                    head,
                    "constraint_tag_malformed",
                    operand.failure,
                    tag_end=operand.end,
                    resume=head.end(),
                )
            target = _split_reference(operand.segments)
            if target is None:
                return _leave_in_place(
                    head,
                    "constraint_tag_malformed",
                    _reference_failure(operand.segments),
                    tag_end=operand.end,
                    resume=operand.end,
                )
            parsed = ParsedFk(target_table=target[0], target_column=target[1])
            if parsed not in foreign_keys:
                foreign_keys.append(parsed)
            return operand.end, ""

        return _leave_in_place(
            head,
            "constraint_tag_unrecognised",
            "not_an_exact_pk_or_fk_tag",
            tag_end=head.end(),
            resume=head.end(),
        )

    kept: list[str] = []
    cursor = 0
    # Rebuilding the description by hand rather than with re.sub: an understood
    # tag's extent is decided by the operand scan, which reaches past the head
    # match, and re.sub can only replace what the pattern itself matched. The loop
    # always terminates because a resume index is never below the head's own end,
    # which is at least three characters past where the search started.
    while (head := _TAG_HEAD.search(comment, cursor)) is not None:
        kept.append(comment[cursor : head.start()])
        cursor, replacement = _read_tag(head)
        kept.append(replacement)
    kept.append(comment[cursor:])

    return ParsedComment(
        description="".join(kept).strip(),
        is_pk_member=is_pk_member,
        foreign_keys=tuple(foreign_keys),
        is_not_null=is_not_null,
    )


def assemble_constraints(
    columns: Sequence[tuple[str, ParsedComment]],
) -> tuple[PrimaryKey | None, list[ForeignKey]]:
    """Turn per-column parse results into table-level constraint records.

    Args:
        columns: ``(column_name, parse_comment(...) result)`` pairs in
            ``DESCRIBE`` order — that order defines the composite PK's column
            order, so callers must not sort or re-group them.

    Returns:
        ``(primary_key, foreign_keys)``. ``primary_key`` is ``None`` when no
        column carried ``@pk`` (rather than an empty ``PrimaryKey``, which would
        claim the table has a key with no columns). ``foreign_keys`` holds one
        single-column record per ``@fk(...)`` tag; a composite FK appears as
        several records, ungrouped.
    """
    pk_columns = [name for name, parsed in columns if parsed.is_pk_member]
    primary_key = (
        PrimaryKey(columns=pk_columns, source=EnrichmentSource.DETERMINISTIC, confidence=1.0) if pk_columns else None
    )
    foreign_keys = [
        ForeignKey(
            column=name,
            target_table=fk.target_table,
            target_column=fk.target_column,
            source=EnrichmentSource.DETERMINISTIC,
            confidence=1.0,
        )
        for name, parsed in columns
        for fk in parsed.foreign_keys
    ]
    return primary_key, foreign_keys


def _scan_operand(comment: str, open_bracket: int) -> _Operand:
    """Read the bracketed operand of an ``@fk`` tag off the comment.

    The operand ends at the first ``)`` that is **not** inside a quoted segment,
    and ``.`` separates segments only outside one. Both questions need the same
    quote-aware walk, so this returns the segments it passed as well as the end it
    found — but returns them RAW, leaving validation and decoding to
    ``_split_reference``. Nothing here judges what a segment says; a scan that
    finds its terminator succeeds even when the operand is unusable, because
    knowing the tag's extent is what lets it be reported once and left in place.

    Args:
        comment: The whole comment.
        open_bracket: Index of the ``(`` immediately after ``@fk``.

    Returns:
        The operand, or a failure naming the delimiter that was never closed.
    """
    segments: list[str] = []
    start = open_bracket + 1
    position = start
    while position < len(comment):
        character = comment[position]
        if character == ")":
            segments.append(comment[start:position])
            return _Operand(segments=tuple(segments), end=position + 1, failure=None)
        if character == ".":
            segments.append(comment[start:position])
            position += 1
            start = position
            continue
        if character == '"':
            closing = _scan_quoted(comment, position)
            if closing is None:
                return _Operand(segments=(), end=len(comment), failure="fk_operand_unterminated_quote")
            position = closing
            continue
        position += 1
    return _Operand(segments=(), end=len(comment), failure="fk_operand_unterminated_bracket")


def _scan_quoted(text: str, start: int) -> int | None:
    """Find the end of the delimited identifier that opens at ``start``.

    A doubled ``""`` is content, not a terminator, so the walk is greedy in
    exactly the way a SQL lexer's is: in ``"a""`` the pair is an escaped quote and
    the identifier is still open at the end of the input, which is what makes an
    odd run of quotes an unterminated-quote error rather than a name that happens
    to parse. Being the only place quotes are counted, this is also what the
    per-segment decoder uses to insist a quoted segment is quoted all through.

    Args:
        text: The string being scanned.
        start: Index of the opening ``"``.

    Returns:
        The index just past the closing ``"``, or ``None`` when the identifier is
        never closed.
    """
    position = start + 1
    while position < len(text):
        if text[position] != '"':
            position += 1
            continue
        if text[position + 1 : position + 2] == '"':
            position += 2
            continue
        return position + 1
    return None


def _decode_segment(segment: str) -> str | None:
    """Decode one raw operand segment into the identifier it names.

    Quoting is per segment and independent, but a segment is either *entirely*
    quoted or entirely bare: ``"a"b`` is rejected rather than read as ``ab``,
    since concatenating a quoted and a bare half would store a name nobody wrote.
    Whitespace around a segment is padding; whitespace inside a bare one is not an
    identifier character.

    Args:
        segment: The raw text between two separators — quotes in place, padding
            not yet dropped.

    Returns:
        The identifier, with quotes removed and ``""`` collapsed to ``"``. May be
        the EMPTY string, which ``""`` spells and no identifier is, so callers
        test truthiness rather than ``None``. ``None`` means the segment is not a
        single quoted or bare identifier at all.
    """
    segment = segment.strip()
    if segment.startswith('"'):
        if _scan_quoted(segment, 0) != len(segment):
            return None
        return segment[1:-1].replace('""', '"')
    if not _UNQUOTED_SEGMENT.fullmatch(segment):
        return None
    return segment


def _split_reference(segments: Sequence[str]) -> tuple[str, str] | None:
    """Split an ``@fk(...)`` reference into ``(target_table, target_column)``.

    The LAST TWO segments are ``TABLE.COLUMN``; anything before them is
    qualification (catalog/database/schema) and is dropped, so
    ``db.public.orders.order_id`` and ``orders.order_id`` both resolve to
    ``("orders", "order_id")``, whichever segments are quoted. That rule mirrors
    ``parse_referred_column``.

    Unlike that splitter — which splits without validating because each of its
    callers degrades differently — this one **rejects** a reference it cannot turn
    into a join condition by returning ``None``. There is one caller and one sane
    outcome: a reference naming no column (``orders``), an empty name (``orders.``)
    or a segment that is not an identifier (``orders.order id``) is a malformed
    tag. Qualification segments are validated too, even though their value is
    discarded, because a bad one still means the author mis-spelled the tag.

    Args:
        segments: The raw segments from ``_scan_operand``.

    Returns:
        ``(target_table, target_column)``, decoded, or ``None`` when the reference
        is malformed. ``_reference_failure`` names which case.
    """
    decoded: list[str] = []
    for segment in segments:
        identifier = _decode_segment(segment)
        if not identifier:
            return None
        decoded.append(identifier)
    if len(decoded) < 2:
        return None
    target_table, target_column = decoded[-2:]
    return target_table, target_column


def _reference_failure(segments: Sequence[str]) -> str:
    """Name why a reference could not be used, for the operator log.

    Args:
        segments: The raw segments ``_split_reference`` rejected.

    Returns:
        A stable reason string for the ``constraint_tag_malformed`` warning. The
        warning reaches operators rather than the connector author, so it names
        the rule that was broken, not just the fact that something was.
    """
    for segment in segments:
        if not _decode_segment(segment):
            return _segment_failure(segment)
    return "fk_operand_not_table_dot_column"


def _segment_failure(segment: str) -> str:
    """Name why one segment is not a usable identifier.

    Args:
        segment: The raw segment ``_decode_segment`` rejected or decoded to
            nothing.

    Returns:
        A stable reason string.
    """
    # Two ways to name nothing: no text at all between the separators
    # ("@fk(orders.)"), or an explicitly empty delimited identifier ('@fk(""."c")').
    if not segment.strip() or _decode_segment(segment) == "":
        return "fk_operand_empty_identifier"
    # Any quote left in a segment the decoder rejected means the segment is part
    # quoted and part not — the decoder accepts every segment that is one
    # complete delimited identifier.
    if '"' in segment:
        return "fk_operand_segment_partially_quoted"
    return "fk_operand_segment_requires_quoting"
