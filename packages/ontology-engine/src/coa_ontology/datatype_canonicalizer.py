# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Canonicalize XSD datatype tokens in an ontology graph.

XSD datatype names are case-sensitive and rdflib's ``XSD`` namespace does zero
validation — it appends whatever string it is given to ``…/XMLSchema#``. So a
free-text or hand-edited range like ``xsd:datetime`` (lowercase ``t``),
``xsd:timestamp``, or ``xsd:varchar`` becomes an *undefined* datatype URI that:

  * silently passes ``rdflib`` parse + our tier-1 validator (HermiT treats an
    unrecognized datatype URI as an opaque user datatype, so it does NOT error —
    unlike ``xsd:date``/``xsd:time`` which ARE recognized-but-unsupported and
    make HermiT abort), and
  * slips past the accept-time normalizer in ``proposals.py`` (which matches only
    the exact-cased ``XSD.dateTime``/``date``/``time``), reaching Ontop as a bogus
    type.

This canonicalizer runs at the ingest / proposal-write boundary and rewrites any
XSD-namespaced datatype token to its correctly-cased, valid equivalent — but only
when we can resolve it to a *known* XSD datatype (by case-folded local name) or a
recognized SQL/free-text alias. A token we cannot resolve is **left untouched**.

The recognized set is the authoritative XML Schema Part 2 built-in datatype list
(https://www.w3.org/TR/xmlschema11-2/#built-in-datatypes): the 19 primitives, the
built-in derived types, and the XSD 1.1 additions. Coercing every *unrecognized*
``xsd:`` token to ``xsd:string`` — the prior behavior — silently corrupted valid
but rarely-emitted types (``xsd:NCName``, ``xsd:NMTOKEN``, ``xsd:anySimpleType``,
``xsd:dayTimeDuration`` …), and because the canonicalizer runs at the *write* /
persist boundary (not before the reasoner) that corruption is irreversible on
S3. Leaving them alone is safe: an unknown ``xsd:`` URI is opaque to HermiT (it
does NOT abort, unlike recognized-but-map-excluded types — see tier-1), and the
canonicalizer's job is to *repair* malformed tokens, not to enforce a whitelist.

This is complementary to the tier-1 HermiT substitution: that one rewrites
*valid-but-OWL2-map-excluded* types (``xsd:date``/``time``/``gYear*``/
``duration``) on the reasoner's throwaway copy only; this one repairs
*malformed/mis-cased/aliased* tokens in the stored ontology itself.
"""

from __future__ import annotations

import logging

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD
from rdflib.term import Node

log = logging.getLogger(__name__)

# Correctly-cased XSD datatype local names we recognize — the authoritative
# XML Schema Part 2 built-in datatype set (https://www.w3.org/TR/xmlschema11-2/
# #built-in-datatypes): 19 primitives + built-in derived types + XSD 1.1 additions.
# Any token whose case-folded local name matches one of these is rewritten to the
# canonical spelling; anything NOT here (and not a known alias below) is left
# untouched rather than coerced to xsd:string. This set is a superset of the OWL 2
# datatype map — the extra temporal types (date/time/gYear*/duration) are valid XSD
# even though they sit outside the OWL 2 map; canonicalization is orthogonal to
# reasoner support (the tier-1 substitution handles the OWL 2 map on its own copy).
_CANONICAL_XSD_LOCALNAMES: frozenset[str] = frozenset(
    {
        # --- 19 primitives (XSD Part 2 §3.3) ---
        "string",
        "boolean",
        "decimal",
        "float",
        "double",
        "duration",
        "dateTime",
        "time",
        "date",
        "gYearMonth",
        "gYear",
        "gMonthDay",
        "gDay",
        "gMonth",
        "hexBinary",
        "base64Binary",
        "anyURI",
        "QName",
        "NOTATION",
        # --- built-in derived: string family (XSD Part 2 §3.4) ---
        "normalizedString",
        "token",
        "language",
        "Name",
        "NCName",
        "NMTOKEN",
        "NMTOKENS",
        "ENTITY",
        "ENTITIES",
        "ID",
        "IDREF",
        "IDREFS",
        # --- built-in derived: numeric family ---
        "integer",
        "nonNegativeInteger",
        "nonPositiveInteger",
        "positiveInteger",
        "negativeInteger",
        "long",
        "int",
        "short",
        "byte",
        "unsignedLong",
        "unsignedInt",
        "unsignedShort",
        "unsignedByte",
        # --- XSD 1.1 additions ---
        "dateTimeStamp",
        "dayTimeDuration",
        "yearMonthDuration",
    }
)

# case-folded → canonical spelling (built once).
_CASEFOLD_TO_CANONICAL: dict[str, str] = {n.casefold(): n for n in _CANONICAL_XSD_LOCALNAMES}

# Common SQL / free-text aliases that are not XSD local names at all → the XSD
# type they should map to. Case-folded keys.
_ALIAS_TO_CANONICAL: dict[str, str] = {
    "datetime": "dateTime",
    "timestamp": "dateTime",
    "timestamptz": "dateTime",
    "timestamp_with_timezone": "dateTime",
    "varchar": "string",
    "char": "string",
    "text": "string",
    "nvarchar": "string",
    "clob": "string",
    "bool": "boolean",
    "number": "decimal",
    "numeric": "decimal",
    "bigint": "long",
    "smallint": "short",
    "tinyint": "byte",
    "real": "double",
    "uuid": "string",
    "blob": "hexBinary",
    "binary": "hexBinary",
}

_XSD_PREFIX = str(XSD)  # http://www.w3.org/2001/XMLSchema#


def _canonical_for(uri: URIRef) -> URIRef | None:
    """Return the canonical XSD URIRef for ``uri`` if it needs rewriting.

    Returns ``None`` when ``uri`` is not an XSD-namespaced token or is already a
    valid, correctly-cased XSD datatype (no change needed).
    """
    s = str(uri)
    if not s.startswith(_XSD_PREFIX):
        return None
    local = s[len(_XSD_PREFIX) :]
    if local in _CANONICAL_XSD_LOCALNAMES:
        return None  # already valid + correctly cased
    folded = local.casefold()
    canonical = _CASEFOLD_TO_CANONICAL.get(folded) or _ALIAS_TO_CANONICAL.get(folded)
    if canonical is not None:
        return URIRef(_XSD_PREFIX + canonical)
    # Unknown XSD-namespaced token (e.g. xsd:garbagetype, or a valid-but-unlisted
    # type we don't enumerate) — leave it untouched. Coercing to xsd:string here
    # would silently and irreversibly corrupt legitimate datatypes on the persisted
    # ontology; an unrecognized xsd: URI is opaque to HermiT (no reasoner abort), so
    # there is no correctness reason to rewrite it. Repair known tokens only.
    return None


def _rewrite_is_value_safe(literal: Literal, canonical: URIRef) -> bool:
    """True when ``literal``'s lexical form is valid for the ``canonical`` datatype.

    The alias table maps SQL type names onto XSD types with far NARROWER lexical
    spaces than the string-ish tokens they replace (``bigint`` → ``xsd:long``,
    ``number`` → ``xsd:decimal``, ``timestamp`` → ``xsd:dateTime``). A value that
    was legal as an opaque ``xsd:bigint``-tagged string is not necessarily legal
    as an ``xsd:long``: rewriting the datatype alone turns ``"abc"^^xsd:bigint``
    into the ill-typed ``"abc"^^xsd:long``.

    That matters more here than it would elsewhere because this module runs at the
    WRITE boundary. Its own docstring is explicit that corruption at that point is
    irreversible on S3, which is why an unrecognized token is left alone — the
    same argument applies to a recognized alias whose value does not fit, and this
    guard extends the policy to cover it. The offending token stays as it was
    (still opaque to HermiT, so no reasoner abort) rather than becoming a literal
    with no value in SPARQL semantics, which silently drops out of result sets and
    can flip an ontology to OWL 2 DL inconsistent on a later run.

    Args:
        literal: The typed literal whose datatype would be rewritten.
        canonical: The datatype it would be rewritten to.

    Returns:
        ``False`` when the rewrite would produce an ill-typed literal.
    """
    return not Literal(str(literal), datatype=canonical, normalize=False).ill_typed


def _scan_datatype_offenders(
    graph: Graph,
) -> list[tuple[Node, Node, Node, URIRef, URIRef, bool, bool]]:
    """Find every triple carrying a malformed/mis-cased/aliased XSD token.

    Single source of truth for BOTH detection and repair, so the two can never
    disagree about what counts as "malformed" (a datatype flagged by
    ``detect_datatype_issues`` is exactly one ``canonicalize_datatypes``
    considers, and vice versa). A token ``_canonical_for`` leaves alone — a
    non-XSD URI, a valid correctly-cased type (incl. ``xsd:date``), or an unknown
    ``xsd:`` token — is neither reported nor rewritten.

    Returns tuples ``(s, p, o, found_uri, canonical_uri, is_literal,
    value_safe)`` where ``found_uri`` is the offending datatype and
    ``canonical_uri`` its replacement. ``is_literal`` is True when the datatype
    sits on a typed literal object (``"x"^^xsd:datetime``) rather than on a
    datatype-URI object (``rdfs:range xsd:datetime`` / ``rr:datatype
    xsd:datetime``). ``value_safe`` is False when rewriting a typed literal to
    ``canonical_uri`` would make it ill-typed — such an offender is still
    REPORTED (the token is genuinely malformed and the user should see it) but is
    not rewritten; see :func:`_rewrite_is_value_safe`.
    """
    offenders: list[tuple[Node, Node, Node, URIRef, URIRef, bool, bool]] = []
    for s, p, o in list(graph):
        if isinstance(o, URIRef):
            canonical = _canonical_for(o)
            if canonical is not None and canonical != o:
                offenders.append((s, p, o, o, canonical, False, True))
        elif isinstance(o, Literal) and o.datatype is not None:
            canonical = _canonical_for(o.datatype)
            if canonical is not None and canonical != o.datatype:
                offenders.append((s, p, o, o.datatype, canonical, True, _rewrite_is_value_safe(o, canonical)))
    return offenders


def canonicalize_datatypes(graph: Graph) -> list[tuple[str, str]]:
    """Rewrite malformed / mis-cased / aliased XSD datatype tokens in ``graph``.

    Mutates ``graph`` in place. Rewrites both datatype URIs used as triple
    objects (e.g. ``rdfs:range xsd:datetime``, ``rr:datatype xsd:datetime``) and
    literal datatypes (``"x"^^xsd:datetime``). Returns the list of
    ``(before, after)`` URI strings that were changed (empty when the graph was
    already clean), so callers can log what they touched.

    Only the datatype IRI changes: a literal's lexical form is carried over
    verbatim, and a rewrite that would leave the literal ill-typed is SKIPPED (the
    triple is left exactly as it was). ``detect_datatype_issues`` still reports
    those tokens, so they remain visible to the user even though this function
    declines to touch them.
    """
    changes: list[tuple[str, str]] = []
    for s, p, o, found, canonical, is_literal, value_safe in _scan_datatype_offenders(graph):
        if not value_safe:
            # Rewriting would produce an ill-typed literal. Leave the token alone:
            # this runs at the persist boundary, so the corruption would be
            # irreversible on S3 — the same reason unrecognized tokens are left
            # untouched. An unknown xsd: URI stays opaque to HermiT (no reasoner
            # abort), whereas an ill-typed literal has no value in SPARQL
            # semantics and can flip the ontology to OWL 2 DL inconsistent.
            log.warning(
                "datatype_alias_incompatible_with_value",
                extra={"found": str(found), "canonical": str(canonical), "subject": str(s), "predicate": str(p)},
            )
            continue
        graph.remove((s, p, o))
        if is_literal:
            # normalize=False is load-bearing: constructing a Literal with a
            # datatype rdflib understands rewrites the lexical form to that
            # datatype's canonical spelling, so a timestamp→dateTime repair
            # silently turned "2024-01-15" into "2024-01-15T00:00:00". This
            # function repairs the TOKEN; the value must survive byte-for-byte.
            graph.add((s, p, Literal(str(o), datatype=canonical, normalize=False)))
        else:
            graph.add((s, p, canonical))
        changes.append((str(found), str(canonical)))
    return changes


def detect_datatype_issues(graph: Graph) -> list[dict[str, str]]:
    """Report malformed/mis-cased/aliased XSD datatype tokens WITHOUT mutating.

    Read-only counterpart to :func:`canonicalize_datatypes`, used by the
    validator to surface offending tokens to the user (who then consents to a
    repair) rather than rewriting them silently. Returns one dict per offending
    triple: ``{subject, predicate, found, canonical, repairable}`` — the
    subject/predicate locate the offending element, ``found`` is the malformed
    datatype IRI and ``canonical`` its proposed replacement. Empty when the graph
    is clean.

    ``repairable`` is ``"false"`` when the token is malformed but the repair would
    leave the literal ill-typed (e.g. ``"abc"^^xsd:bigint`` → ``xsd:long``), in
    which case :func:`canonicalize_datatypes` deliberately leaves the triple
    alone. Such offenders are still reported: the token IS wrong and the user
    should see it, they just need to fix the value rather than consent to a
    mechanical rewrite.
    """
    return [
        {
            "subject": str(s),
            "predicate": str(p),
            "found": str(found),
            "canonical": str(canonical),
            "repairable": "true" if value_safe else "false",
        }
        for s, p, _o, found, canonical, _is_literal, value_safe in _scan_datatype_offenders(graph)
    ]


def canonicalize_turtle(turtle: str) -> tuple[str, list[tuple[str, str]]]:
    """Parse ``turtle``, canonicalize datatypes, and re-serialize.

    Returns ``(canonicalized_turtle, changes)``. When nothing changed the
    original ``turtle`` string is returned unmodified (so a clean ontology keeps
    its byte-for-byte serialization).
    """
    g = Graph()
    g.parse(data=turtle, format="turtle")
    changes = canonicalize_datatypes(g)
    if not changes:
        return turtle, []
    return g.serialize(format="turtle"), changes


def detect_datatype_issues_in_turtle(turtle: str | None) -> list[dict[str, str]]:
    """Parse ``turtle`` (ontology or R2RML) and return datatype offenders.

    Convenience wrapper over :func:`detect_datatype_issues` for a serialized
    string; ``None`` / empty / unparseable input yields ``[]`` (detection must
    never raise — a validator that can't parse simply reports no datatype issue).
    """
    if not turtle:
        return []
    g = Graph()
    try:
        g.parse(data=turtle, format="turtle")
    except Exception:
        return []
    return detect_datatype_issues(g)
