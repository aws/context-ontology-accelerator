# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared "ingest an ontology" helper.

Given a ``(GraphStore, VectorStore)`` pair, a byte payload carrying an
ontology (Turtle / RDF-XML / OWL-XML / JSON-LD), and a target namespace,
this module performs the full acceptance pipeline that is shared by:

  - ``POST /ontologies/import`` (file upload)
  - ``POST /ontologies/import-from-url`` (URL fetch)
  - ``POST /ontology/proposals/{id}/accept`` (inducer output)

Pipeline steps (see :func:`ingest_ontology` for details):

  1. Parse the payload with rdflib, resolving the actual format.
  2. Resolve the ontology's identifier (``ontology_id``) — the value
     comes from the caller when known (proposal accept, explicit
     import with ``uri=...``) or is derived from the Turtle's
     ``owl:Ontology`` subject.
  3. (Optional) Run tier-1 validators on the parsed graph.
  4. Conflict check against the Dynamo registry — 409 by default,
     wipe + reload when ``replace=True``.
  5. Catalog projection through the ``GraphStore`` abstraction:
     ``create_ontology`` + ``load_turtle`` + ``store_class`` +
     ``store_property`` + ``update_ontology``.
  6. Lexical embedding accumulation via the ``VectorStore``.
  7. Dynamo namespace registry write (last, so a failed ingest does
     not leave a phantom registry row behind).

The helper returns a dict with per-step counts and status fields so
callers can echo the summary in HTTP responses without re-deriving it.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import defusedxml
import rdflib.plugins.parsers.rdfxml as _rdfxml_parser
from coa_common import DEFAULT_EMBED_MODEL_ID
from coa_common.constants import VOCAB_URI
from defusedxml.expatreader import DefusedExpatParser
from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef
from rdflib.namespace import SKOS

from coa_ontology import dynamo_store
from coa_ontology.stores.base import GraphStore, VectorStore

# ── Security: XXE protection (CWE-611/CWE-502, Issue #572) ──────────────────
# Patch Python's stdlib XML parsers (xml.sax, xml.etree, xml.dom, minidom) at
# import time to reject external entity resolution and external DTDs. rdflib
# delegates RDF/XML parsing to xml.sax, so this must run BEFORE any
# Graph.parse(). Safe to call repeatedly (idempotent).
defusedxml.defuse_stdlib()


# defuse_stdlib() forbids *all* XML entities — including the benign INTERNAL
# string entities that standard OWL ontologies (BFO, IOF, and most OBO
# vocabularies) declare in their DOCTYPE for namespace prefixes, e.g.
#
#     <!DOCTYPE rdf:RDF [ <!ENTITY bfo "http://purl.obolibrary.org/obo/"> ]>
#     ... rdf:about="&bfo;BFO_0000001" ...
#
# Those are literal-string macros with no SYSTEM/PUBLIC identifier: they read
# no files and make no network calls, so they are not an XXE vector. Forbidding
# them (EntitiesForbidden) makes a large class of real ontologies un-ingestable.
#
# rdflib's RDF/XML parser calls make_parser() via its own module-level name, so
# override just that factory: RDF/XML ingest permits INTERNAL entity
# declarations while STILL blocking EXTERNAL entities and external DTDs
# (forbid_external=True) — the actual XXE / CWE-611 vector. Entity-expansion
# ("billion laughs") stays bounded by CPython 3.12's built-in expat
# amplification limit. All other stdlib XML parsing remains fully hardened by
# defuse_stdlib() above.
def _make_rdfxml_parser(*_args: Any, **_kwargs: Any) -> DefusedExpatParser:
    return DefusedExpatParser(forbid_dtd=False, forbid_entities=False, forbid_external=True)


_rdfxml_parser.make_parser = _make_rdfxml_parser

log = logging.getLogger(__name__)

# Registry-row status for an async upload that has been accepted but whose
# background parse+embed hasn't finished. The upload path writes a row with this
# status up-front so it appears in the ontologies list immediately (with an
# in-progress indicator); ``ingest_ontology(overwrite_stub=True)`` then replaces
# it with the fully-ingested row. Kept in sync with the "deleting" status the
# router uses for the delete-in-progress indicator.
ONTOLOGY_STATUS_INGESTING = "ingesting"

# Predicates that carry a human-readable definition/description for a class
# or property. Different ontology families use different ones: schema.org and
# most W3C vocabularies use rdfs:comment, but FIBO and other formal/financial
# ontologies (and SKOS vocabularies, OBO, etc.) put their definitions in
# skos:definition. Capturing only rdfs:comment silently drops FIBO's
# definitions, leaving its classes embedded as bare labels — which tanks
# grounding-match quality against them. OBO's IAO definition is included as
# a third common source.
_DEFINITION_PREDICATES = (
    RDFS.comment,
    SKOS.definition,
    URIRef("http://purl.obolibrary.org/obo/IAO_0000115"),
)

# Annotation predicates an append-mode re-ingest SUPERSEDES rather than
# accumulates. A proposal re-induced after steward curation carries the
# authored description/synonyms only, but the earlier accepted proposal's
# AI-generated ``rdfs:comment`` / ``skos:altLabel`` were already live in the
# same named graph — and RDF set semantics only dedup IDENTICAL literals, so
# both generations ended up on the class and the model had to pick one.
#
# Each live predicate maps to the HISTORY predicate its displaced values move
# to: the previous generation stays inspectable on the subject (``GET
# /graph/class`` surfaces it as ``superseded``) without being live content that
# serve/embeddings read. The rule is deliberately field-scoped: descriptions
# and alternative labels are authored-wins; relationship axioms
# (``rdfs:range``, ``rdfs:subClassOf``, ``owl:ObjectProperty``, R2RML) are NOT
# here, so inferred join paths survive a re-accept — an accuracy benchmark
# showed those beating the authored-only set.
_COA = Namespace(VOCAB_URI)
SUPERSEDED_COMMENT = _COA.supersededComment
SUPERSEDED_DEFINITION = _COA.supersededDefinition
SUPERSEDED_ALT_LABEL = _COA.supersededAltLabel
_SUPERSEDED_ANNOTATION_PREDICATES: dict[URIRef, URIRef] = {
    RDFS.comment: SUPERSEDED_COMMENT,
    SKOS.definition: SUPERSEDED_DEFINITION,
    URIRef("http://purl.obolibrary.org/obo/IAO_0000115"): SUPERSEDED_DEFINITION,
    SKOS.altLabel: SUPERSEDED_ALT_LABEL,
}


def _definitions(graph: Graph, subject) -> list[str]:
    """Collect definition/description literals for a subject, deduplicated and order-stable.

    Reads across the predicates ontologies actually use (``rdfs:comment``,
    ``skos:definition``, and the OBO IAO definition property).
    """
    seen: list[str] = []
    for pred in _DEFINITION_PREDICATES:
        for o in graph.objects(subject, pred):
            text = str(o).strip()
            if text and text not in seen:
                seen.append(text)
    return seen


def _supersede_incoming_embeddings(
    vector_store: VectorStore,
    namespace: str,
    ontology_id: str,
    subjects: Iterable[Any],
) -> int:
    """Retire the stale embeddings of ``subjects`` before an append re-embeds them.

    Delegates to :meth:`VectorStore.delete_embeddings_for_entities`; backends
    without the method (older adapters, test doubles) are treated as having
    nothing to retire. A backend failure raises :class:`IngestStoreError`. This
    runs as the FIRST write of an append, before the graph is touched, so a
    failure here aborts with nothing to roll back; continuing instead would
    leave the index disagreeing with the graph (stale AI text still searchable)
    — the exact state this step exists to prevent.

    The delete is scoped by ``ontology_id`` in addition to ``entity_uri``. A
    single namespace index can carry embeddings for multiple ontologies
    (foundational reloads with ``allow_append=True``, explicit-target merges),
    and the same IRI can appear in more than one; an unscoped delete would
    take the sibling ontology's embedding down with the current one and
    re-create it under the wrong owner.
    """
    delete = getattr(vector_store, "delete_embeddings_for_entities", None)
    uris = [str(s) for s in subjects]
    if delete is None or not uris or not ontology_id:
        return 0
    try:
        removed = int(delete(uris, ontology_id=ontology_id, namespace=namespace) or 0)
    except Exception as e:
        raise IngestStoreError(f"embedding supersession failed: {e}") from e
    log.info(
        "ingest: append retired %d stale embeddings across %d subjects (namespace=%s ontology_id=%s)",
        removed,
        len(uris),
        namespace,
        ontology_id,
    )
    return removed


def _supersede_incoming_annotations(graph_store: GraphStore, ontology_id: str, subjects: Iterable[Any]) -> int:
    """Clear the live descriptions/alt-labels of ``subjects`` before an append writes them anew.

    Delegates to :meth:`GraphStore.supersede_annotations`; backends without the
    method (older adapters, test doubles) are treated as having nothing to
    supersede. A backend failure is raised as :class:`IngestStoreError` rather
    than swallowed: quietly falling back to the additive behaviour would
    reintroduce the very defect this step exists to remove.

    Ordering note: step 4a (``_supersede_incoming_embeddings``) runs before
    this, so a failure here leaves the stale embeddings already retired. The
    incoming classes are then without a live description or embedding until
    a re-accept converges — an availability gap, not data loss, and the
    accepted proposal enters ``accept_failed`` which is re-acceptable.
    """
    supersede = getattr(graph_store, "supersede_annotations", None)
    subject_uris = [str(s) for s in subjects]
    if supersede is None or not subject_uris:
        return 0
    try:
        removed = int(
            supersede(
                ontology_uri=ontology_id,
                subject_uris=subject_uris,
                predicates={str(p): str(h) for p, h in _SUPERSEDED_ANNOTATION_PREDICATES.items()},
            )
            or 0
        )
    except Exception as e:
        raise IngestStoreError(f"annotation supersession failed: {e}") from e
    log.info(
        "ingest: append superseded %d live annotation triples across %d subjects (ontology_id=%s)",
        removed,
        len(subject_uris),
        ontology_id,
    )
    return removed


# Formats we can parse with rdflib, keyed by the ``format`` field that
# the catalog schema exposes. Falls back to ``turtle`` if a caller
# supplies an unexpected value.
_RDFLIB_FORMATS = {
    "turtle": "turtle",
    "ttl": "turtle",
    "rdf-xml": "xml",
    "rdf+xml": "xml",
    "owl-xml": "xml",
    "xml": "xml",
    "json-ld": "json-ld",
    "jsonld": "json-ld",
}


def _sniff_rdf_format(text: str) -> str | None:
    """Best-effort RDF serialization detection from the payload's first bytes.

    Used as a fallback when the declared format fails to parse (e.g. a server
    content-negotiates JSON-LD for a source the catalog records as rdf+xml).
    Returns an rdflib format string, or None when nothing is recognised.
    """
    stripped = text.lstrip()
    if not stripped:
        return None
    if stripped[0] in "{[":
        return "json-ld"
    if stripped.startswith("<?xml") or stripped.startswith("<rdf:") or "<rdf:RDF" in stripped[:512]:
        return "xml"
    # Turtle/N-Triples both parse under "turtle"; @prefix / @base are turtle tells.
    if stripped.startswith("@prefix") or stripped.startswith("@base") or stripped.startswith("PREFIX"):
        return "turtle"
    return None


class IngestError(Exception):
    """Base class for ingest failures. Subclasses map to HTTP status codes."""


class IngestParseError(IngestError):
    """Payload was not valid RDF in the declared / detected format. → 422."""


class IngestValidationError(IngestError):
    """Tier-1 validation found blocking issues. → 422.

    The full findings list is attached as ``self.findings``.
    """

    def __init__(self, message: str, findings: list[Any]):
        """Initialize the error with a message and the blocking validation findings.

        Args:
            message: Human-readable description of the validation failure.
            findings: The Tier-1 findings that blocked ingest; stored on ``self.findings``.
        """
        super().__init__(message)
        self.findings = findings


class IngestConflictError(IngestError):
    """An ontology with the same id already exists in this namespace. → 409."""


class IngestStoreError(IngestError):
    """A call to the graph store failed mid-ingest. → 502."""


@dataclass
class IngestMetadata:
    """Caller-supplied hints. Any ``None`` fields fall back to RDF-derived values."""

    ontology_id: str | None = None  # IRI; if omitted, derived from owl:Ontology
    title: str | None = None
    description: str | None = None
    ontology_type: str = "user_created"  # foundational | induced | user_created
    version: str | None = None
    version_iri: str | None = None
    license: str | None = None
    license_tier: str | None = None
    source_registry: str | None = None
    source_url: str | None = None
    format: str | None = None  # "turtle" | "rdf-xml" | "json-ld" | ...
    domain_tags: list[str] | None = None
    imports: list[str] | None = None
    file_path: str | None = None  # local path of the uploaded bytes
    source: str = "import"  # import | import-from-url | proposal-accept


def _class_datasource_map_from_r2rml(r2rml_content: bytes | str | None) -> dict[str, str]:
    """Map each R2RML-mapped class URI → its datasource-id, parsed from R2RML.

    Traversal (per the R2RML the inducer emits)::

        <TriplesMap> a rr:TriplesMap ;
            coa:datasourceId "ds-abc123" ;
            rr:subjectMap [ rr:class <ClassURI> ] .

    A class present in the returned dict is R2RML-mapped — i.e. it is the
    ``rr:class`` of some TriplesMap, hence SQL-backed and answerable by the
    structured Tier-2 path. The TWO consumers key on different aspects of the
    same entry, so they must be decoupled:

    * Neptune ``coa:isMapped`` marker (Ontop T-Box answerability) keys on
      MAP MEMBERSHIP — "is this class the rr:class of a TriplesMap" — NOT on the
      datasource-id value. Ontop can resolve a mapped class even without a
      per-class datasource id (it has a ``default`` source fallback).
    * AOSS ``data_source_id`` (NL->SQL routing) uses the VALUE. NL->SQL needs a
      concrete datasource id to route; a class with no id is (correctly) dropped
      by that path's presence filter.

    Therefore a TriplesMap whose ``rr:class`` has NO ``coa:datasourceId`` is
    still recorded, with an EMPTY-STRING value: it counts as mapped (Ontop can
    answer it) but carries no routable id (NL->SQL skips it). Keying membership
    on the id — the previous behavior — wrongly hid a SQL-backed class from
    Ontop whenever its TriplesMap lacked the (routing-only) id annotation.

    NOTE — MULTIPLE MAPPINGS PER CLASS: a class can be the ``rr:class`` of more
    than one TriplesMap (mapped from several datasources / tables — e.g. two
    same-named tables in different datasources both mint the same class IRI).
    This dict keeps the LAST NON-EMPTY datasource-id seen for such a class (a
    later id-less TriplesMap does not blank out an id already found), so the
    other source is dropped for NL->SQL routing (isMapped/Ontop are unaffected —
    they key on membership, not the id). Tracked as a follow-up: widen the return
    type to ``dict[str, set[str]]`` and federate multi-source classes instead of
    pinning one. Until then a multi-source class may route to a single source.

    Returns an empty dict when ``r2rml_content`` is None/empty or unparseable
    (fail-soft: a class with no derivable mapping is simply treated as unmapped).
    """
    if not r2rml_content:
        return {}
    text = r2rml_content.decode("utf-8") if isinstance(r2rml_content, bytes) else r2rml_content
    if not text.strip():
        return {}

    rr = Namespace("http://www.w3.org/ns/r2rml#")
    scl = Namespace(VOCAB_URI)
    try:
        rg = Graph()
        rg.parse(data=text, format="turtle")
    except Exception as e:  # noqa: BLE001 — malformed R2RML must not fail ingest
        log.warning("r2rml parse failed while deriving class→datasource map: %s", e)
        return {}

    mapping: dict[str, str] = {}
    for tmap in rg.subjects(RDF.type, rr.TriplesMap):
        ds_id = str(rg.value(tmap, scl.datasourceId) or "")
        for subj_map in rg.objects(tmap, rr.subjectMap):
            cls_uri = rg.value(subj_map, rr["class"])
            if cls_uri is None:
                continue
            key = str(cls_uri)
            # Membership (key present) == mapped, independent of the ds-id, so a
            # TriplesMap with no coa:datasourceId still marks its class mapped
            # (empty-string value). For a class mapped by MULTIPLE TriplesMaps,
            # keep the LAST NON-EMPTY ds-id, and never let an id-less TriplesMap
            # blank out an id already found. (See this function's docstring.)
            if key not in mapping:
                mapping[key] = ds_id  # first sighting (may be "")
            elif ds_id:
                mapping[key] = ds_id  # later non-empty id wins; id-less is ignored
    return mapping


def ingest_ontology(
    graph_store: GraphStore,
    vector_store: VectorStore,
    content: bytes | str,
    *,
    namespace: str,
    metadata: IngestMetadata | None = None,
    validate: bool = False,
    allow_append: bool = False,
    source_proposal_id: str | None = None,
    r2rml_content: bytes | str | None = None,
    overwrite_stub: bool = False,
) -> dict:
    """Run the full ingest pipeline.

    Two modes:

    - **Create** (``allow_append=False``, default): enforces single-upload
      semantics. If an ontology with the same ``ontology_id`` already
      exists in the namespace, raises :class:`IngestConflictError`
      without touching any store. This is the mode the ``/upload`` HTTP
      endpoint uses.

    - **Append** (``allow_append=True``): the ontology is allowed to
      already exist; the incoming payload is merged into the existing
      per-ontology named graph. This is the mode the proposal-accept
      flow uses so multiple induced proposals can be merged under a
      single user-chosen target ontology URI. Specifically:

        * ``create_ontology`` is skipped when the target already
          exists (otherwise it would duplicate the ``dct:created``
          / ``dct:modified`` timestamp triples on each accept).
        * ``store_class`` / ``store_property`` / ``load_turtle`` are
          naturally additive — SPARQL ``INSERT DATA`` and GSP
          ``POST`` merge triples. Identical triples dedup via RDF
          set semantics.
        * Descriptions and alternative labels are the exception: for the
          subjects present in the incoming payload, their live
          ``rdfs:comment`` / ``skos:definition`` / ``skos:altLabel`` are
          removed first (``supersede_annotations``) so the incoming
          values REPLACE them. Other subjects and all relationship
          axioms remain additive.
        * The registry row's ``source_proposals`` list grows with
          every append, and class/property/axiom/embedding counts
          accumulate via DynamoDB ``ADD``. Counts are cumulative
          across all appends — they do not collapse to the dedup'd
          graph cardinality.

    ``source_proposal_id`` is recorded on the registry row when the
    append mode is driven by a proposal. It's the tracking key so the
    row knows which proposals contributed.

    Returns a dict with the shape::

        {
          "ontology_id": str,
          "namespace": str,
          "graph_uri": str,
          "class_count": int,            # counts in THIS payload
          "property_count": int,
          "axiom_count": int,
          "turtle_load": {...},
          "embeddings": {...},
          "registry": {...},             # current registry row after the write
          "appended": bool,              # True if this was an append
          "superseded_annotation_count": int,  # live comment/altLabel triples
                                         # replaced on append; 0 on create
          "superseded_embedding_count": int,   # stale embeddings of the incoming
                                         # subjects retired on append; 0 on create
        }
    """
    md = metadata or IngestMetadata()
    text = content.decode("utf-8") if isinstance(content, bytes) else content

    # ── 1. Parse ────────────────────────────────────────────────────────
    # The declared format (md.format) can be wrong: a server may content-negotiate
    # and return e.g. JSON-LD even though the catalog/record says rdf+xml (FOAF does
    # this). Try the declared format first, then fall back to a format sniffed from
    # the actual bytes so a stale format hint doesn't drop the whole load.
    declared_fmt = _RDFLIB_FORMATS.get((md.format or "turtle").lower(), "turtle")
    sniffed_fmt = _sniff_rdf_format(text)
    candidates = [declared_fmt] + ([sniffed_fmt] if sniffed_fmt and sniffed_fmt != declared_fmt else [])
    g = Graph()
    last_err: Exception | None = None
    for fmt in candidates:
        try:
            g.parse(data=text, format=fmt)
            last_err = None
            break
        except Exception as e:  # noqa: BLE001 — try the next candidate format
            last_err = e
    if last_err is not None:
        raise IngestParseError(f"Invalid {md.format or 'turtle'} input: {last_err}") from last_err

    # ── 1b. Strip any caller-supplied coa:isMapped triples ──────────────
    # ``coa:isMapped`` is a TRUST-BOUNDARY marker: the serve Tier-2 filter treats
    # a class carrying it as SQL-answerable. It must be derivable ONLY from the
    # R2RML (server-side, via store_class) — never asserted by the payload. Left
    # in place, a user-uploaded/edited ontology could FORGE ``<Class> coa:isMapped
    # true`` and, since the raw Turtle is bulk-loaded verbatim (step 6), that
    # forged triple would be queryable by the serve filter and force an
    # unmapped/unbacked class into the structured-query prompt. Strip every
    # ``(?, coa:isMapped, ?)`` here so only store_class can (re)emit it.
    stripped_markers = _strip_ismapped_triples(g)
    if stripped_markers:
        log.warning(
            "ingest: stripped %d caller-supplied coa:isMapped triple(s) — the marker is "
            "server-derived from R2RML only (ontology_id=%s namespace=%s)",
            stripped_markers,
            md.ontology_id or "?",
            namespace,
        )

    # ── 2. Resolve ontology_id ──────────────────────────────────────────
    ontology_id = md.ontology_id or _extract_ontology_iri(g)
    if not ontology_id:
        raise IngestParseError(
            "Could not determine ontology_id — no owl:Ontology subject in the "
            "payload and no ontology_id supplied by the caller."
        )

    # Only project NAMED classes/properties. Foundational ontologies like
    # FIBO declare anonymous (blank-node) class expressions — anonymous
    # owl:Restriction / owl:unionOf constructs typed as owl:Class — which
    # have no IRI to catalog or ground against. Including them caused
    # store_class to throw on the blank-node id and abort the entire
    # module ingest (FIBO Contracts / ClientsAndAccounts loaded 0 classes).
    def _named(subjects):
        return {s for s in subjects if isinstance(s, URIRef)}

    classes = _named(g.subjects(RDF.type, OWL.Class)) | _named(g.subjects(RDF.type, RDFS.Class))
    dt_props = _named(g.subjects(RDF.type, OWL.DatatypeProperty))
    obj_props = (
        _named(g.subjects(RDF.type, OWL.ObjectProperty))
        | _named(g.subjects(RDF.type, RDF.Property))
        | _named(g.subjects(RDF.type, OWL.AnnotationProperty))
        | _named(g.subjects(RDF.type, OWL.FunctionalProperty))
    ) - dt_props

    title = md.title or _first_literal(g, ontology_id, RDFS.label) or _local_name(ontology_id)
    description = md.description or _first_literal(g, ontology_id, RDFS.comment)

    # ── Tier-2 answerability signal ─────────────────────────────────────
    # Parse the (optional) R2RML mapping into a class-URI → datasource-id map.
    # A class present in this map is R2RML-mapped (the rr:class of some
    # TriplesMap → SQL-backed → answerable by Ontop / NL->SQL). Two consumers
    # derive from this single map:
    #   * Neptune T-Box (store_class): emits an ``coa:isMapped`` boolean marker,
    #     derived purely from map MEMBERSHIP (the ds-id string is not stored in
    #     the graph). The serve T-Box context filters on it so unmapped
    #     (unstructured / foundational) classes don't pollute the structured
    #     prompt.
    #   * AOSS embeddings (_accumulate_embeddings): writes the datasource-id
    #     STRING into each class doc's ``data_source_id`` field; the serve
    #     NL->SQL path filters on its presence.
    # The R2RML is NOT loaded into the graph — only scanned here to derive this.
    # NOTE: a class can be the rr:class of MULTIPLE TriplesMaps (mapped from
    # several datasources). This map keeps only the LAST datasource-id seen for
    # such a class. That is sufficient for the current use (membership = mapped;
    # presence of any ds-id = answerable). Multi-source routing (federate instead
    # of pinning one source) is a planned follow-up — widen to dict[str, set[str]].
    class_to_datasource: dict[str, str] = _class_datasource_map_from_r2rml(r2rml_content)

    # ── 3. Tier-1 validators (optional) ─────────────────────────────────
    if validate:
        findings = _run_tier1_validators(g)
        blocking = [f for f in findings if getattr(f, "severity", None) == "error"]
        if blocking:
            raise IngestValidationError(
                f"Tier-1 validation found {len(blocking)} blocking issue(s).",
                findings=findings,
            )

    # ── 4. Conflict / append branching ─────────────────────────────────
    existing = dynamo_store.get_ontology_registry(namespace, ontology_id)
    # The async upload path writes an "ingesting" STUB row up-front (so the row
    # shows immediately in the ontologies list with a spinner). That stub is NOT
    # a real conflict — this same ingest call is what fills it in. Treat a stub
    # as overwritable so the fresh-write path below replaces it cleanly.
    existing_is_stub = existing is not None and existing.get("status") == ONTOLOGY_STATUS_INGESTING
    if existing and not allow_append and not (overwrite_stub and existing_is_stub):
        raise IngestConflictError(
            f"Ontology {ontology_id!r} already exists in namespace {namespace!r}. Delete it first to re-upload."
        )
    # Append only when a REAL (non-stub) row exists — never treat our own stub as
    # an append target (that would skip create_ontology + accumulate onto zeros).
    appending = bool(existing) and allow_append and not existing_is_stub

    # ── 4a. Retire stale embeddings on append ─────────────────────────────
    # FIRST write of the append, deliberately: the vector store is the least
    # reliable dependency here, so it is the one to fail on while NOTHING has
    # been written to the graph. Ordering this after the graph supersession (as
    # a first cut did) meant a vector-store outage left the graph carrying the
    # steward's text while the index still served the AI text's embedding —
    # exactly the graph/index disagreement this step exists to prevent. The
    # embeddings being retired are those of the subjects this payload
    # re-defines (re-embedded in step 7); the earlier accept indexed the AI
    # description, and AOSS has no
    # client-supplied _id, so delete-then-add is the only way to REPLACE rather
    # than accumulate (closes the #594/#824 duplication caveat for re-accept).
    # Same scoping as 4b: exactly the incoming subjects; co-merged proposals'
    # embeddings are untouched. Skipped on a fresh ingest — nothing to retire.
    superseded_embedding_count = 0
    if appending:
        superseded_embedding_count = _supersede_incoming_embeddings(
            vector_store, namespace, ontology_id, classes | obj_props | dt_props
        )

    # ── 4b. Supersede annotations on append ───────────────────────────────
    # Every write below is additive (INSERT DATA / GSP POST), so before them
    # drop the LIVE descriptions and alt-labels of exactly the subjects this
    # payload is about to (re)define. The incoming values then replace the
    # earlier generation instead of standing beside it. Scoped to the incoming
    # subjects only, so a co-merged proposal's classes in the same ontology are
    # untouched; and to annotation predicates only, so relationship axioms
    # survive (see _SUPERSEDED_ANNOTATION_PREDICATES). Skipped on a fresh
    # ingest — there is nothing live to supersede.
    superseded_count = 0
    if appending:
        superseded_count = _supersede_incoming_annotations(graph_store, ontology_id, classes | obj_props | dt_props)

    # ── 5. Catalog projection ───────────────────────────────────────────
    _s1 = time.perf_counter()
    if not appending:
        graph_store.create_ontology(
            {
                "uri": ontology_id,
                "title": title,
                "ontology_type": md.ontology_type,
                "description": description or f"Imported ontology ({md.source})",
                "version": md.version,
                "license": md.license,
                "license_tier": md.license_tier,
                "source_registry": md.source_registry,
                "source_url": md.source_url,
                "format": md.format or "turtle",
                "domain_tags": md.domain_tags or [],
                "imports": md.imports or [],
            }
        )

    mapped_class_count = sum(1 for cls_uri in classes if str(cls_uri) in class_to_datasource)
    # Batch the class projection into a few chunked INSERTs when the backend
    # supports it (Neptune DB), instead of one HTTP round-trip per class. Falls
    # back to per-class store_class for backends without the batch method (NA).
    class_specs: list[dict[str, Any]] = [
        {
            "class_uri": str(cls_uri),
            "labels": [str(o) for o in g.objects(cls_uri, RDFS.label)],
            "comments": _definitions(g, cls_uri),
            "super_classes": [str(o) for o in g.objects(cls_uri, RDFS.subClassOf) if o in classes],
            "is_mapped": str(cls_uri) in class_to_datasource,
        }
        for cls_uri in classes
    ]
    # Roll back the ontology on any class/property projection failure — but ONLY
    # on a fresh ingest (an append's pre-existing state is live and must not be
    # wiped on a transient failure). Neptune DB has no multi-statement
    # transaction, so a batch that fails on chunk 2 of 3 (or a per-item loop that
    # throws partway) leaves earlier writes committed; without this the graph is
    # left with a partial class/property set. This mirrors the load_turtle
    # rollback below. NOTE: this no-rollback-on-partial-projection gap is
    # PRE-EXISTING — the previous per-class store_class loop had the same
    # exposure; batching only changed the granularity of what's already
    # committed at the point of failure, so this fix closes a latent gap rather
    # than one introduced by batching.
    try:
        if hasattr(graph_store, "store_classes_batch"):
            graph_store.store_classes_batch(ontology_uri=ontology_id, classes=class_specs)
        else:
            for spec in class_specs:
                graph_store.store_class(ontology_uri=ontology_id, **spec)
    except Exception as e:
        if not appending:
            try:
                graph_store.delete_ontology(ontology_id)
            except Exception:  # pragma: no cover — best effort
                log.exception("cleanup delete_ontology after class projection failure")
        raise IngestStoreError(f"class projection failed: {e}") from e
    # Observability for the Tier-2 answerability marker. A class is hidden from
    # the structured-query path unless it is mapped, so a mismatch here is the
    # single signal that a namespace will be (partly or wholly) "dark" to Tier-2.
    # Emitted always so operators can see it; WARNING when R2RML was supplied
    # (i.e. a structured ingest) yet ZERO classes came out mapped — the canary
    # for "structured namespace accepted but Tier-2 will return nothing" (e.g.
    # malformed/empty R2RML, or a catalog that dropped every rr:class).
    if class_to_datasource and mapped_class_count == 0:
        log.warning(
            "ingest: 0 of %d classes are Tier-2-mapped despite R2RML being supplied "
            "(ontology_id=%s namespace=%s) — the namespace will be DARK to the structured "
            "query path; check the R2RML for rr:class TriplesMaps.",
            len(classes),
            ontology_id,
            namespace,
        )
    else:
        log.info(
            "ingest: Tier-2 mapped %d/%d classes (ontology_id=%s namespace=%s)",
            mapped_class_count,
            len(classes),
            ontology_id,
            namespace,
        )

    prop_specs: list[dict[str, Any]] = []
    for prop_uri in obj_props | dt_props:
        prop_defs = _definitions(g, prop_uri)
        prop_specs.append(
            {
                "prop_uri": str(prop_uri),
                "label": str(next(g.objects(prop_uri, RDFS.label), "")),
                "comment": prop_defs[0] if prop_defs else "",
                "domains": [str(o) for o in g.objects(prop_uri, RDFS.domain) if o in classes],
                "ranges": [str(o) for o in g.objects(prop_uri, RDFS.range) if o in classes],
            }
        )
    # Same fresh-ingest rollback as the class projection above: a partial
    # property write (batch chunk or per-item loop failing partway) must not
    # leave a half-projected ontology behind.
    try:
        if hasattr(graph_store, "store_properties_batch"):
            graph_store.store_properties_batch(ontology_uri=ontology_id, properties=prop_specs)
        else:
            for spec in prop_specs:
                graph_store.store_property(ontology_uri=ontology_id, **spec)
    except Exception as e:
        if not appending:
            try:
                graph_store.delete_ontology(ontology_id)
            except Exception:  # pragma: no cover — best effort
                log.exception("cleanup delete_ontology after property projection failure")
        raise IngestStoreError(f"property projection failed: {e}") from e

    log.info(
        "ingest: step1 catalog projection %.0fms (classes=%d props=%d)",
        (time.perf_counter() - _s1) * 1000,
        len(classes),
        len(obj_props) + len(dt_props),
    )

    # ── 6. Raw Turtle bulk-load ─────────────────────────────────────────
    # Strip any triple whose object is an empty blank node before
    # serialising for the backend. Opus-generated induced ontologies
    # sometimes emit ``rdfs:subClassOf [ ]`` to mean "no superclass",
    # which rdflib parses happily but Neptune's strict RDF 1.1 parser
    # rejects as ``Invalid syntax`` on GSP POST.
    stripped = _strip_dangling_bnode_objects(g)
    if stripped:
        log.info("ingest: stripped %d dangling-bnode triples before load_turtle", stripped)

    # Always serialise from the cleaned rdflib graph — we cannot reuse
    # the caller's original bytes verbatim because those may still
    # contain the dangling BNode constructs we just removed.
    try:
        turtle = g.serialize(format="turtle")
    except Exception as e:
        raise IngestStoreError(f"Could not serialize to Turtle for bulk load: {e}") from e

    _s2 = time.perf_counter()
    try:
        load_result = graph_store.load_turtle(ontology_uri=ontology_id, turtle=turtle)
    except Exception as e:
        # Roll back the catalog writes — but ONLY if this is a fresh
        # ingest. On append the pre-existing state is live and
        # non-recoverable; we must not wipe it on a transient load
        # failure.
        if not appending:
            try:
                graph_store.delete_ontology(ontology_id)
            except Exception:  # pragma: no cover — best effort
                log.exception("cleanup delete_ontology after load_turtle failure")
        raise IngestStoreError(f"load_turtle failed: {e}") from e
    log.info(
        "ingest: step2 load_turtle %.0fms (bytes=%d, status=%s)",
        (time.perf_counter() - _s2) * 1000,
        len(turtle.encode("utf-8")),
        load_result.get("status") if isinstance(load_result, dict) else "?",
    )

    # ── 7. Embedding accumulation (shared vector index per namespace) ──
    _s3 = time.perf_counter()
    embedding_result = _accumulate_embeddings(
        vector_store=vector_store,
        graph=g,
        ontology_id=ontology_id,
        classes=classes,
        obj_props=obj_props,
        dt_props=dt_props,
        namespace=namespace,
        class_to_datasource=class_to_datasource,
    )
    log.info(
        "ingest: step3 accumulate_embeddings %.0fms (%s)",
        (time.perf_counter() - _s3) * 1000,
        embedding_result.get("status"),
    )

    if embedding_result.get("status") == "error":
        raise IngestStoreError(f"Embedding accumulation failed: {embedding_result.get('error', 'unknown')}")

    # ── 8. Graph-store ontology metadata update ─────────────────────────
    # On a fresh ingest we overwrite the counts on the graph node to
    # reflect what was just loaded. On append we leave the graph node's
    # counts alone — the DDB registry holds the cumulative state, which
    # is more accurate than the first-proposal snapshot.
    if not appending:
        graph_store.update_ontology(
            ontology_id,
            {
                "class_count": len(classes),
                "property_count": len(obj_props) + len(dt_props),
                "axiom_count": len(g),
                "parse_status": "ok",
            },
        )

    graph_uri = _derive_graph_uri_for_response(graph_store, ontology_id, namespace, load_result)

    # ── 9. Namespace registry write (fully-ingested marker) ─────────────
    dynamo_store.put_namespace_meta(
        namespace,
        data={
            "backend": os.getenv("WORKBENCH_BACKEND", "opensearch_neptune"),
            "embedding_model_id": embedding_result.get("model_id")
            or os.getenv("BEDROCK_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL_ID),
            "embedding_dimensions": int(os.getenv("BEDROCK_EMBED_DIMENSIONS", "1024")),
            "embedding_index": embedding_result.get("index"),
            "graph_uri_prefix": _graph_uri_prefix_for_response(graph_uri),
        },
    )
    if appending:
        # Atomic accumulate: bump counts, append this proposal id to the
        # source_proposals list, refresh timestamps. Preserve the row's
        # ``source`` / ``ontology_type`` / ``title`` / etc. from the
        # initial create.
        registry_row = dynamo_store.extend_ontology_registry(
            namespace,
            ontology_id,
            add_class_count=len(classes),
            add_property_count=len(obj_props) + len(dt_props),
            add_axiom_count=len(g),
            add_embedding_count=embedding_result.get("count") or 0,
            source_proposal=source_proposal_id,
            # Flip parse_status to "ok" on the append/foundational-load path too.
            # The initial create writes "pending"; without setting it here the row
            # (and the ontologies-table ingest indicator) stays "pending" forever
            # after a successful load. The fresh-write branch below sets it too.
            parse_status="ok",
            last_fetched=datetime.now(UTC).isoformat(),
        )
    else:
        registry_row = dynamo_store.put_ontology_registry(
            namespace,
            ontology_id,
            {
                "uri": ontology_id,
                "title": title,
                "description": description,
                "ontology_type": md.ontology_type,
                "version": md.version,
                "version_iri": md.version_iri,
                "license": md.license,
                "license_tier": md.license_tier,
                "source_registry": md.source_registry,
                "source_url": md.source_url,
                "format": md.format or "turtle",
                "domain_tags": md.domain_tags or [],
                "imports": md.imports or [],
                "file_path": md.file_path,
                "source": md.source,
                "source_proposals": [source_proposal_id] if source_proposal_id else [],
                "graph_uri": graph_uri,
                "embedding_index": embedding_result.get("index"),
                "embedding_model_id": embedding_result.get("model_id"),
                "embedding_count": embedding_result.get("count"),
                "class_count": len(classes),
                "property_count": len(obj_props) + len(dt_props),
                "axiom_count": len(g),
                "parse_status": "ok",
                "last_fetched": datetime.now(UTC).isoformat(),
            },
        )

    return {
        "ontology_id": ontology_id,
        "namespace": namespace,
        "graph_uri": graph_uri,
        "class_count": len(classes),
        # Tier-2 answerability: how many of `class_count` carry coa:isMapped.
        # 0 (with a non-empty ontology) means the namespace is dark to the
        # structured-query path — callers/tests can assert on this.
        "mapped_class_count": mapped_class_count,
        "superseded_annotation_count": superseded_count,
        "superseded_embedding_count": superseded_embedding_count,
        "property_count": len(obj_props) + len(dt_props),
        "axiom_count": len(g),
        "turtle_load": load_result,
        "embeddings": embedding_result,
        "registry": _strip_dynamo_internals(registry_row),
        "appended": appending,
    }


# ── Embedding readiness ──────────────────────────────────────────────────

# AOSS (OpenSearch Serverless) is eventually consistent on the vector index:
# a freshly-written embedding is not immediately returned by a search. Any
# flow that loads an ontology and then relies on its embeddings being
# searchable (grounding recall against a freshly-loaded external ontology,
# serve-time vector retrieval) must wait until every embedding it wrote is
# queryable. ``STALL_TIMEOUT_S`` bounds how long we wait *without observing
# progress* — it resets every time another embedding becomes searchable, so a
# large ontology that's syncing slowly-but-steadily is never cut off; only a
# genuinely stuck index trips it. Tunable via env for slower collections.
EMBEDDING_SEARCHABLE_STALL_TIMEOUT_S = float(os.getenv("INGEST_EMBEDDING_READY_TIMEOUT_S", "120"))
EMBEDDING_SEARCHABLE_POLL_S = float(os.getenv("INGEST_EMBEDDING_READY_POLL_S", "3"))


def wait_for_embeddings_searchable(
    *,
    vector_store: VectorStore,
    ontology_id: str,
    namespace: str,
    expected_uris: list[str],
    stall_timeout_s: float = EMBEDDING_SEARCHABLE_STALL_TIMEOUT_S,
    poll_s: float = EMBEDDING_SEARCHABLE_POLL_S,
    on_progress: Callable[[int, int], None] | None = None,
) -> bool:
    """Block until every URI in ``expected_uris`` is searchable in the vector index.

    Polls :meth:`VectorStore.list_searchable_entity_uris` (URI-only, vectors
    excluded) and checks that the set of indexed ``entity_uri`` values covers
    the full set written during this ingest. Returns ``True`` once covered.

    ``stall_timeout_s`` is a *no-progress* deadline, not a total wall-clock
    cap: it resets on every iteration that observes more embeddings searchable
    than the previous best. Returns ``False`` only after ``stall_timeout_s``
    elapses with no new embedding appearing — i.e. the index is genuinely
    stuck, not merely slow. This lets a large ontology that's syncing steadily
    take as long as it needs while still bailing out of a truly wedged sync.

    ``on_progress(searchable, total)`` — optional, called once per iteration that
    observes FORWARD PROGRESS. Because this wait has no total wall-clock cap, a
    large-but-healthy sync can outlive the proposal stale-sweep's window; the
    accept worker passes a callback that heartbeats the proposal so a live sync is
    never mistaken for a dead worker and swept out from under itself. Callback
    errors are swallowed: a failed heartbeat must not abort a healthy wait.

    Never raises — a probe error is treated as "no progress this iteration"
    and retried until the stall deadline. Returns ``False`` on a genuine stall.

    IMPORTANT — a ``False`` return means LOSS, not benign lag. This wait runs
    only AFTER :func:`_accumulate_embeddings` has confirmed the vector store
    acknowledged every write (``bulk_with_retry`` raises on any doc that fails
    to land — see #173). So the docs are known-acknowledged before we start
    polling; if after ``stall_timeout_s`` of no forward progress some URIs are
    STILL not searchable, that is an index that will not converge (a dropped
    write that slipped past acknowledgement, or an index-health problem), NOT a
    write that simply hasn't happened yet. Callers on a correctness-critical
    path (proposal accept) MUST treat ``False`` as fatal rather than "lagged"
    (see :func:`_run_accept_proposal`). The bool return is retained because
    non-critical callers legitimately proceed on a slow index, but the meaning
    of ``False`` is "did not converge", not "fine, just slow".
    ``expected_uris`` is the exact list returned under ``embeddings.entity_uris``
    by :func:`ingest_ontology`.
    """
    expected = {u for u in expected_uris if u}
    if not expected:
        return True

    best_present = -1  # forces the first successful probe to count as progress
    stall_deadline = time.perf_counter() + stall_timeout_s
    attempt = 0
    while True:
        attempt += 1
        missing = expected
        try:
            present = set(vector_store.list_searchable_entity_uris(ontology_id, namespace=namespace))
            searchable = len(expected & present)
            missing = expected - present
            if not missing:
                log.info(
                    "ingest: all %d embeddings searchable for %s (after %d poll(s))",
                    len(expected),
                    ontology_id,
                    attempt,
                )
                return True
            # Reset the stall deadline whenever forward progress is observed.
            if searchable > best_present:
                best_present = searchable
                stall_deadline = time.perf_counter() + stall_timeout_s
                log.info(
                    "ingest: embedding sync progress for %s — %d/%d searchable (stall timer reset)",
                    ontology_id,
                    searchable,
                    len(expected),
                )
                if on_progress is not None:
                    try:
                        on_progress(searchable, len(expected))
                    except Exception as cb_err:  # noqa: BLE001 — heartbeat is best-effort
                        log.warning("ingest: embedding-sync progress callback failed: %s", cb_err)
        except Exception as e:  # noqa: BLE001 — transient probe failure → retry until the stall deadline
            log.warning("ingest: embedding readiness probe failed (attempt %d): %s", attempt, e)

        if time.perf_counter() >= stall_deadline:
            log.error(
                "ingest: embedding sync did NOT converge for %s — no progress for %.0fs, "
                "%d of %d still not searchable. The writes were acknowledged, so this is a "
                "convergence/loss failure, not benign lag; correctness-critical callers treat "
                "this as fatal.",
                ontology_id,
                stall_timeout_s,
                len(missing),
                len(expected),
            )
            return False
        time.sleep(poll_s)


def _is_index_not_found(exc: Exception) -> bool:
    """True if ``exc`` indicates the vector index doesn't exist.

    Backend-agnostic (no hard opensearchpy dependency here): match on the
    exception class name / message so a missing index — which for a *deletion*
    wait means the embeddings are definitively gone — is recognized without
    coupling ``ingest`` to a specific vector backend's exception types.
    """
    name = type(exc).__name__
    msg = str(exc).lower()
    return (
        name in ("NotFoundError", "IndexNotFoundError")
        or "index_not_found" in msg
        or "no such index" in msg
        or "resource_not_found" in msg
    )


def wait_for_embeddings_deleted(
    *,
    vector_store: VectorStore,
    ontology_id: str,
    namespace: str,
    deleted_uris: list[str],
    stall_timeout_s: float = EMBEDDING_SEARCHABLE_STALL_TIMEOUT_S,
    poll_s: float = EMBEDDING_SEARCHABLE_POLL_S,
) -> bool:
    """Block until none of ``deleted_uris`` are searchable in the vector index.

    The inverse of :func:`wait_for_embeddings_searchable`: after an ontology's
    embeddings are deleted, AOSS is eventually consistent, so a query issued
    immediately after the delete could still return the just-deleted vectors.
    This polls :meth:`VectorStore.list_searchable_entity_uris` and waits until
    every URI in ``deleted_uris`` has disappeared from the index — i.e. the
    deletions are actually reflected in search results.

    ``deleted_uris`` is the exact set of entity_uris that were present for the
    ontology at delete time (captured before the delete). ``stall_timeout_s``
    is a *no-progress* deadline (resets whenever more of the deleted URIs
    disappear), not a total wall-clock cap — so a large deletion that's
    propagating steadily takes as long as it needs while a genuinely wedged
    index still bails out.

    Never raises — a probe error is treated as "no progress this iteration"
    and retried; a stall is logged but does not fail the caller (the docs are
    deleted durably; only their search-result visibility lagged).
    """
    remaining_target = {u for u in deleted_uris if u}
    if not remaining_target:
        return True

    best_gone = -1  # forces the first successful probe to count as progress
    stall_deadline = time.perf_counter() + stall_timeout_s
    attempt = 0
    while True:
        attempt += 1
        still_present = remaining_target
        try:
            present = set(vector_store.list_searchable_entity_uris(ontology_id, namespace=namespace))
            still_present = remaining_target & present
            gone = len(remaining_target) - len(still_present)
            if not still_present:
                log.info(
                    "delete: all %d embeddings no longer searchable for %s (after %d poll(s))",
                    len(remaining_target),
                    ontology_id,
                    attempt,
                )
                return True
            # Reset the stall deadline whenever more deleted URIs disappear.
            if gone > best_gone:
                best_gone = gone
                stall_deadline = time.perf_counter() + stall_timeout_s
                log.info(
                    "delete: embedding removal progress for %s — %d/%d no longer searchable (stall timer reset)",
                    ontology_id,
                    gone,
                    len(remaining_target),
                )
        except Exception as e:  # noqa: BLE001
            # A "not found" (missing index) means there's nothing left to be
            # searchable — the embeddings are definitively gone, so treat it as
            # success rather than burning the whole stall timeout retrying. Any
            # other probe error is transient → retry until the stall deadline.
            if _is_index_not_found(e):
                log.info(
                    "delete: vector index gone for %s — embeddings not searchable (after %d poll(s))",
                    ontology_id,
                    attempt,
                )
                return True
            log.warning("delete: embedding removal probe failed (attempt %d): %s", attempt, e)

        if time.perf_counter() >= stall_deadline:
            log.warning(
                "delete: embedding removal stalled for %s — no progress for %.0fs, "
                "%d of %d still searchable (deleted durably; search visibility lagged)",
                ontology_id,
                stall_timeout_s,
                len(still_present),
                len(remaining_target),
            )
            return False
        time.sleep(poll_s)


def ingest_ontology_and_wait(
    graph_store: GraphStore,
    vector_store: VectorStore,
    content: bytes | str,
    *,
    namespace: str,
    metadata: IngestMetadata | None = None,
    validate: bool = False,
    allow_append: bool = False,
    source_proposal_id: str | None = None,
    on_embeddings_sync: Callable[[], None] | None = None,
    overwrite_stub: bool = False,
) -> dict:
    """Run :func:`ingest_ontology`, then block until its embeddings are searchable.

    The single entry point for ontology *loading* (foundational/grounding
    load, async S3 import, sync upload) — every loader writes embeddings the
    same way and must wait out the AOSS eventual-consistency window before a
    grounding/serve call can rely on them. Consolidates the previously
    duplicated ``ingest_ontology(...) → extract entity_uris → wait`` blocks so
    the readiness contract lives in exactly one place.

    ``on_embeddings_sync`` is an optional hook invoked immediately before the
    wait begins — callers that expose an "embeddings syncing" status (e.g. the
    async ingest job's ``EMBEDDINGS_SYNC`` state) record it here. It runs only
    when there is at least one embedding to wait for.

    Returns the :func:`ingest_ontology` result dict unchanged, so callers keep
    reading ``result["registry"]`` / ``result["embeddings"]`` exactly as before.

    Note: proposal acceptance deliberately does NOT route through here — it
    owns its own status machine (``accepting`` → ``embeddings_sync`` →
    ``accepted``) and calls :func:`wait_for_embeddings_searchable` directly.
    """
    result = ingest_ontology(
        graph_store=graph_store,
        vector_store=vector_store,
        content=content,
        namespace=namespace,
        metadata=metadata,
        validate=validate,
        allow_append=allow_append,
        source_proposal_id=source_proposal_id,
        overwrite_stub=overwrite_stub,
    )

    embedded_uris = (result.get("embeddings") or {}).get("entity_uris", [])
    if embedded_uris and on_embeddings_sync is not None:
        on_embeddings_sync()
    wait_for_embeddings_searchable(
        vector_store=vector_store,
        ontology_id=result["ontology_id"],
        namespace=namespace,
        expected_uris=embedded_uris,
    )
    return result


# ── Helpers ─────────────────────────────────────────────────────────────


def _extract_ontology_iri(g: Graph) -> str | None:
    """Return the first ``owl:Ontology`` subject, if any."""
    for s in g.subjects(RDF.type, OWL.Ontology):
        return str(s)
    return None


def _first_literal(g: Graph, subject: str, predicate) -> str | None:
    from rdflib import URIRef

    for o in g.objects(URIRef(subject), predicate):
        s = str(o).strip()
        if s:
            return s
    return None


def _local_name(uri: str) -> str:
    tail = uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    return tail or uri


def _split_camel(s: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", s)


def _run_tier1_validators(g: Graph) -> list[Any]:
    """Run the blocking-tier validators on a parsed rdflib graph."""
    from coa_ontology.validation.validators.tier1 import (
        ConnectivityValidator,
        ConsistencyValidator,
        SHACLValidator,
        TaxonomyCycleValidator,
    )

    findings: list[Any] = []
    for v in (
        ConsistencyValidator(),
        TaxonomyCycleValidator(),
        ConnectivityValidator(),
        SHACLValidator(),
    ):
        try:
            findings.extend(v.validate(g))
        except Exception as e:  # pragma: no cover — tier-1 validators shouldn't raise
            log.warning("validator %s raised: %s", v.__class__.__name__, e)
    return findings


def _accumulate_embeddings(
    *,
    vector_store: VectorStore,
    graph: Graph,
    ontology_id: str,
    classes: set,
    obj_props: set,
    dt_props: set,
    namespace: str,
    class_to_datasource: dict[str, str] | None = None,
) -> dict:
    """Embed every class + property and upsert into the namespace's vector index.

    The shared index per namespace is owned by the ``VectorStore`` —
    ``store_embeddings_batch`` carries ``namespace`` on each item so
    backends that shard by namespace (OpenSearch index, Neptune
    Analytics namespace-scoped vertex) write into the correct bucket.
    """
    from coa_ontology.bedrock_embeddings import BedrockEmbeddingClient

    def _text_for(uri) -> str:
        labels = [str(o) for o in graph.objects(uri, RDFS.label)]
        # Pull definitions from rdfs:comment AND skos:definition (FIBO et al.)
        # so the embedding captures the class's meaning, not just its label.
        comments = _definitions(graph, uri)
        # skos:altLabel carries source synonyms — include them so the embedding
        # captures alternative names the source curated.
        alt_labels = [str(o) for o in graph.objects(uri, SKOS.altLabel)]
        label_text = " ".join(_split_camel(lbl) for lbl in labels) or _split_camel(_local_name(str(uri)))
        comment_text = " ".join(comments)
        alt_text = " ".join(alt_labels)
        return (label_text + " " + comment_text + " " + alt_text).strip()

    _SCL_NS = Namespace(VOCAB_URI)

    def _class_text_for(cls_uri) -> tuple[str, str]:
        """Build structured class text, returning ``(embed_text, context_text)``.

        ``embed_text`` is EMBEDDED for retrieval (names/description/columns, no
        sampled values — noise for recall). ``context_text`` is stored, not
        embedded, for the serve NL→SQL prompt: same text plus each column's
        ``allowed values: [...]``. The two are equal when no column is sampled.
        """
        name = str(graph.value(cls_uri, RDFS.label) or _local_name(str(cls_uri)))
        description = str(graph.value(cls_uri, RDFS.comment) or "")
        synonyms = [str(o) for o in graph.objects(cls_uri, SKOS.altLabel)]

        obj_props_list = []
        # (label, dtype, comment, distinct_values)
        data_props_list: list[tuple[str, str, str, list[str]]] = []

        for prop_uri in graph.subjects(RDFS.domain, cls_uri):
            prop_label = str(graph.value(prop_uri, RDFS.label) or _local_name(str(prop_uri)))
            prop_comment = str(graph.value(prop_uri, RDFS.comment) or "")
            prop_range = graph.value(prop_uri, RDFS.range)

            if (prop_uri, RDF.type, OWL.ObjectProperty) in graph:
                range_label = (
                    str(graph.value(prop_range, RDFS.label) or _local_name(str(prop_range))) if prop_range else ""
                )
                obj_props_list.append((prop_label, range_label, prop_comment))
            elif (prop_uri, RDF.type, OWL.DatatypeProperty) in graph:
                dtype = str(prop_range).split("#")[-1] if prop_range else "string"
                distinct_vals = [str(o) for o in graph.objects(prop_uri, _SCL_NS.distinctValues)]
                data_props_list.append((prop_label, dtype, prop_comment, distinct_vals))

        if not data_props_list and not obj_props_list:
            thin = _text_for(cls_uri)
            return thin, thin

        base_parts = [f"Table: {name}"]
        if description:
            base_parts.append(f"Description: {description}")
        if synonyms:
            base_parts.append(f"Synonyms: {', '.join(synonyms)}")
        if obj_props_list:
            rels = ", ".join(
                f"{label}->{target}" + (f" ({comment})" if comment else "") for label, target, comment in obj_props_list
            )
            base_parts.append(f"Relationships: {rels}")

        def _cols(*, rich: bool) -> str:
            # rich=False → embedded vector: column names + comments only (type
            # and sampled values are low-signal for retrieval).
            # rich=True → generation context: adds type + allowed values so the
            # LLM writes correct casts and WHERE literals.
            return ", ".join(
                (f"{label}:{dtype}" if rich else label)
                + (f" ({comment[:100]})" if comment else "")
                + (f" allowed values: [{', '.join(vals[:20])}]" if rich and vals else "")
                for label, dtype, comment, vals in data_props_list
            )

        embed_parts = list(base_parts)
        context_parts = list(base_parts)
        if data_props_list:
            embed_parts.append(f"Columns: {_cols(rich=False)}")
            context_parts.append(f"Columns: {_cols(rich=True)}")

        embed_text = " | ".join(embed_parts)
        context_text = " | ".join(context_parts)
        return embed_text, context_text

    # class → datasource-id map is derived from the R2RML by the caller
    # (``ingest_ontology`` → ``_class_datasource_map_from_r2rml``) and passed in.
    # It is NOT re-derived from ``graph`` here: ``graph`` is the ontology-only
    # payload and never contains R2RML TriplesMaps, so scanning it would always
    # yield an empty map (the bug that left ``data_source_id`` blank on every
    # induced class). A class present in the map is R2RML-mapped; its ds-id
    # string is written to ``data_source_id`` for the serve NL->SQL filter.
    ds_map = class_to_datasource or {}

    # (uri, entity_type, embed_text, context_text, datasource_id)
    # embed_text is EMBEDDED (retrieval vector); context_text is STORED for the
    # serve NL→SQL generation prompt and may carry per-column allowed values.
    entries: list[tuple[str, str, str, str, str]] = []
    for uri in classes:
        embed_text, context_text = _class_text_for(uri)
        entries.append((str(uri), "class", embed_text, context_text, ds_map.get(str(uri), "")))
    for uri in obj_props | dt_props:
        thin = _text_for(uri)
        entries.append((str(uri), "property", thin, thin, ""))

    if not entries:
        return {"status": "skipped", "reason": "no entities", "count": 0}

    try:
        bedrock = BedrockEmbeddingClient(
            region=os.getenv("BEDROCK_REGION", os.getenv("LLM_REGION", "us-east-1")),
            model_id=os.getenv("BEDROCK_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL_ID),
            dimensions=int(os.getenv("BEDROCK_EMBED_DIMENSIONS", "1024")),
        )
        # Embed only the LEAN text — sampled enum values live in context_text and
        # must not enter the retrieval vector.
        texts = [embed_text for (_, _, embed_text, _, _) in entries]
        vectors = bedrock.embed_texts(texts)

        items = []
        for (uri, entity_type, embed_text, context_text, ds_id), vec in zip(entries, vectors, strict=True):
            item = {
                "entity_uri": uri,
                "ontology_id": ontology_id,
                "entity_type": entity_type,
                "embedding_type": "lexical",
                "model_id": bedrock.model_id,
                "namespace": namespace,
                "text": embed_text,
                "vector": vec,
            }
            # Only carry context_text when it adds something beyond the embedded
            # text (i.e. sampled enum values were appended) — keeps storage lean.
            if context_text != embed_text:
                item["context_text"] = context_text
            if ds_id:
                item["data_source_id"] = ds_id
            items.append(item)
        # store_embeddings_batch returns the docs the store confirms it wrote.
        # With bulk_with_retry now raising PartialIndexError on any doc that
        # fails to land (see #173), a partial write reaches the except-block
        # below rather than being reported as success. We derive count and
        # entity_uris from the STORE'S RETURN VALUE, not len(items): the two
        # must be structurally coupled so "count" can never again be the
        # submission count while fewer docs actually indexed.
        stored = vector_store.store_embeddings_batch(items)
        # Prefer the store's acknowledged docs as authoritative. A store that
        # returns a list of docs tells us exactly what landed; anything else
        # (None/legacy, or a scalar count) is not a per-doc acknowledgement, so
        # we fall back to the submitted items (the write either fully succeeded
        # — bulk_with_retry raises otherwise — or the store isn't list-returning).
        acked = stored if isinstance(stored, list) else items
        # A store that acknowledges FEWER docs than submitted, without raising,
        # is still a partial write — surface it loudly rather than silently
        # under-reporting (belt-and-suspenders alongside PartialIndexError).
        if len(acked) < len(items):
            raise RuntimeError(
                f"vector store acknowledged {len(acked)} of {len(items)} embeddings for {ontology_id} "
                f"(partial write); refusing to report success"
            )
        acked_uris = [d.get("entity_uri") for d in acked if isinstance(d, dict) and d.get("entity_uri")]

        # Best-effort: some vector-store implementations expose the target
        # index name via a helper; most do not. We infer it from the
        # OpenSearch-style naming convention when present so the registry
        # and the namespace META row can carry it for discovery.
        index_name: str | None = None
        resolver = getattr(vector_store, "_index_for_namespace", None)
        if callable(resolver):
            try:
                index_name = resolver(namespace)
            except Exception:  # pragma: no cover
                index_name = None
        return {
            "status": "ok",
            "count": len(acked),
            "classes": sum(1 for e in entries if e[1] == "class"),
            "properties": sum(1 for e in entries if e[1] == "property"),
            "model_id": bedrock.model_id,
            "index": index_name,
            # Exact list of entity URIs the store ACKNOWLEDGED writing to the
            # vector index in this call (not merely submitted). Callers (notably
            # proposal-accept) use it to block until every one is searchable,
            # closing the AOSS consistency window.
            "entity_uris": acked_uris,
        }
    except Exception as e:
        log.exception("embedding accumulation failed for %s", ontology_id)
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _wipe(graph_store: GraphStore, vector_store: VectorStore, namespace: str, ontology_id: str) -> None:
    """Remove an ontology's presence across the three stores prior to replace."""
    try:
        vector_store.delete_embeddings_for_ontology(ontology_id, namespace=namespace)
    except Exception:  # pragma: no cover — best effort
        log.exception("wipe: delete_embeddings_for_ontology failed for %s", ontology_id)
    try:
        graph_store.delete_ontology(ontology_id)
    except Exception:
        log.exception("wipe: graph_store.delete_ontology failed for %s", ontology_id)
    dynamo_store.delete_ontology_registry(namespace, ontology_id)


def _derive_graph_uri_for_response(graph_store: GraphStore, ontology_id: str, namespace: str, load_result: Any) -> str:
    """Best-effort resolution of the ontology's named graph URI.

    Prefers the value reported by ``load_turtle`` (Neptune DB returns it
    explicitly). Falls back to the static helper on the Neptune DB store,
    and as a final fallback returns the ontology_id itself (for backends
    that don't model named graphs — Neptune Analytics property graph).
    """
    if isinstance(load_result, dict) and load_result.get("graph_uri"):
        return str(load_result["graph_uri"])
    try:
        from coa_ontology.stores.neptune_db_graph import _ontology_graph_uri

        return _ontology_graph_uri(ontology_id, namespace)
    except Exception:
        return ontology_id


def _strip_dangling_bnode_objects(g: Graph) -> int:
    """Remove triples whose object is a BNode that has no outgoing predicates.

    These arise from Turtle like ``rdfs:subClassOf [ ]`` which rdflib
    parses as ``(subject, rdfs:subClassOf, _:anonBNode)`` where the anon
    BNode carries no further triples. Strict RDF 1.1 parsers (notably
    Neptune DB's GSP endpoint) reject the ``[ ]`` serialisation as
    invalid syntax. Stripping them keeps the graph semantically clean
    ("no known superclass" becomes "no triple") and lets the Turtle
    load without errors.

    Returns the number of triples removed.
    """
    from rdflib import BNode

    removed = 0
    for s, p, o in list(g):
        if isinstance(o, BNode) and not list(g.predicate_objects(o)):
            g.remove((s, p, o))
            removed += 1
    return removed


def _strip_ismapped_triples(g: Graph) -> int:
    """Remove every ``(?, coa:isMapped, ?)`` triple from a parsed payload graph.

    ``coa:isMapped`` is a server-derived TRUST-BOUNDARY marker (see the caller):
    the serve Tier-2 filter matches it to decide a class is SQL-answerable, and
    it must be emitted ONLY by ``store_class`` from the R2RML mapping — never
    accepted from caller-supplied Turtle. Returns the number of triples removed.
    """
    is_mapped_pred = URIRef(f"{VOCAB_URI}isMapped")
    triples = list(g.triples((None, is_mapped_pred, None)))
    for triple in triples:
        g.remove(triple)
    return len(triples)


def _graph_uri_prefix_for_response(graph_uri: str) -> str | None:
    """Return the ``{base}/{ns}`` prefix of a per-ontology graph URI.

    The named-graph scheme is ``{base}/{ns}/{quote(ontology_id)}``, so the
    prefix is everything up to (but not including) the final path
    segment.
    """
    if "/" not in graph_uri:
        return None
    return graph_uri.rsplit("/", 1)[0]


def _strip_dynamo_internals(item: dict | None) -> dict:
    if not item:
        return {}
    return {k: v for k, v in item.items() if k not in ("PK", "SK")}
