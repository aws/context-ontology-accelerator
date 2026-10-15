# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 3 — Semi-automated, flag for review.

Checks:
  - OoPS! pitfall detection (via remote API)
  - Missing labels/comments
  - Competency question evaluation (stub — future LLM integration)
"""

import os

import httpx
from defusedxml.ElementTree import fromstring as safe_fromstring
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef
from rdflib.namespace import SKOS

from coa_ontology.validation.schemas import Severity, ValidationFinding, ValidationTier
from coa_ontology.validation.validators import OntologyValidator


class OoPSValidator(OntologyValidator):
    """Check ontology against OoPS! (OntOlogy Pitfall Scanner).

    Opt-in: the scan sends the serialised ontology (class and property names,
    labels, comments and other literals) to the configured endpoint, so it runs
    only when ``OOPS_ENDPOINT`` is set. There is deliberately no built-in default
    endpoint — a deployment must never transmit its schema to a third party
    without the operator choosing to.
    """

    def validate(self, graph: Graph, **kwargs) -> list[ValidationFinding]:
        """Submit the ontology to the OoPS! pitfall scanner and map pitfalls to findings.

        Strips ``owl:imports`` and posts the RDF/XML serialization to the OoPS!
        REST endpoint named by ``OOPS_ENDPOINT``, parsing either the XML or JSON
        response shape. With no endpoint configured, makes no request.

        Args:
            graph: The ontology graph to scan.
            **kwargs: Unused; accepted for interface compatibility.

        Returns:
            A finding per detected pitfall (warning for IMPORTANT/CRITICAL, else
            info), or a single info finding when no endpoint is configured, or the
            service is unavailable or errors.
        """
        endpoint = os.getenv("OOPS_ENDPOINT", "").strip()
        if not endpoint:
            return [
                ValidationFinding(
                    validator="oops",
                    tier=ValidationTier.tier3_review,
                    severity=Severity.info,
                    code="OOPS_NOT_CONFIGURED",
                    message=(
                        "OoPS! pitfall scan is off — no endpoint is configured, so the ontology was not sent "
                        "anywhere. Set OOPS_ENDPOINT (deploy context oops_endpoint) to an OoPS! REST endpoint "
                        "to enable it."
                    ),
                )
            ]

        findings = []
        # OoPS! self-hosted expects RDF/XML content via JSON API.
        # Strip owl:imports to prevent OoPS from trying to fetch remote ontologies.
        clean = Graph()
        for t in graph:
            if t[1] != OWL.imports:
                clean.add(t)
        rdfxml = clean.serialize(format="xml")

        try:
            xml_payload = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<OOPSRequest>\n"
                "<OntologyUrl></OntologyUrl>\n"
                f"<OntologyContent><![CDATA[{rdfxml}]]></OntologyContent>\n"
                "<Pitfalls></Pitfalls>\n"
                "<OutputFormat></OutputFormat>\n"
                "</OOPSRequest>"
            )
            with httpx.Client(timeout=120) as client:
                resp = client.post(
                    endpoint,
                    content=xml_payload,
                    headers={"Content-Type": "application/xml"},
                )
                resp.raise_for_status()

            # OoPS may return XML despite requesting JSON
            text = resp.text.strip()
            if text.startswith("<"):
                root = safe_fromstring(text)
                for pf_group in root.findall(".//pitfalls"):
                    for pf_el in pf_group:
                        code = pf_el.tag
                        info = pf_el.find("info")
                        title = info.findtext("title", "Unknown pitfall") if info else "Unknown pitfall"
                        importance = info.findtext("importance", "MINOR") if info else "MINOR"
                        resources = [r.text for r in pf_el.findall(".//uri") if r.text]
                        sev = Severity.warning if importance in ("IMPORTANT", "CRITICAL") else Severity.info
                        affected = ", ".join(f"<{u}>" for u in resources[:5])
                        if len(resources) > 5:
                            affected += f" (+{len(resources) - 5} more)"
                        msg = f"{code}: {title}"
                        if affected:
                            msg += f" — {affected}"
                        findings.append(
                            ValidationFinding(
                                validator="oops",
                                tier=ValidationTier.tier3_review,
                                severity=sev,
                                code=f"OOPS_{code}",
                                message=msg,
                                subject=resources[0] if resources else None,
                            )
                        )
            else:
                data = resp.json()
                pitfalls = data.get("pitfalls", {})
                for code, instances in pitfalls.items():
                    for pf in instances:
                        info = pf.get("info", {})
                        importance = info.get("importance", "MINOR")
                        title = info.get("title", "Unknown pitfall")
                        resources = [r.get("uri", "") for r in pf.get("resources", []) if r.get("uri")]
                        sev = Severity.warning if importance in ("IMPORTANT", "CRITICAL") else Severity.info
                        affected_str = ", ".join(f"<{u}>" for u in resources[:5])
                        if len(resources) > 5:
                            affected_str += f" (+{len(resources) - 5} more)"
                        msg = f"{code}: {title}"
                        if affected_str:
                            msg += f" — {affected_str}"
                        findings.append(
                            ValidationFinding(
                                validator="oops",
                                tier=ValidationTier.tier3_review,
                                severity=sev,
                                code=f"OOPS_{code}",
                                message=msg,
                                subject=resources[0] if resources else None,
                            )
                        )

        except httpx.HTTPError:
            findings.append(
                ValidationFinding(
                    validator="oops",
                    tier=ValidationTier.tier3_review,
                    severity=Severity.info,
                    code="OOPS_UNAVAILABLE",
                    message="OoPS! service unavailable — skipping pitfall scan",
                )
            )
        except Exception as e:
            findings.append(
                ValidationFinding(
                    validator="oops",
                    tier=ValidationTier.tier3_review,
                    severity=Severity.info,
                    code="OOPS_ERROR",
                    message=f"OoPS! check failed: {e}",
                )
            )
        return findings


class LabelCompletenessValidator(OntologyValidator):
    """Flag classes and properties missing rdfs:label or rdfs:comment."""

    def validate(self, graph: Graph, **kwargs) -> list[ValidationFinding]:
        """Report classes and properties lacking an ``rdfs:label`` or ``rdfs:comment``.

        Args:
            graph: The ontology graph to inspect for documentation completeness.
            **kwargs: Unused; accepted for interface compatibility.

        Returns:
            An info finding per entity missing a label and per entity missing a
            comment; empty when every class and property is documented.
        """
        findings = []
        for entity_type, type_name in [
            (OWL.Class, "Class"),
            (OWL.ObjectProperty, "ObjectProperty"),
            (OWL.DatatypeProperty, "DatatypeProperty"),
        ]:
            for s in graph.subjects(RDF.type, entity_type):
                if not isinstance(s, URIRef):
                    continue
                labels = list(graph.objects(s, RDFS.label))
                comments = list(graph.objects(s, RDFS.comment))
                if not labels:
                    findings.append(
                        ValidationFinding(
                            validator="label_completeness",
                            tier=ValidationTier.tier3_review,
                            severity=Severity.info,
                            code="MISSING_LABEL",
                            message=f"{type_name} <{s}> is missing rdfs:label",
                            subject=str(s),
                        )
                    )
                if not comments:
                    findings.append(
                        ValidationFinding(
                            validator="label_completeness",
                            tier=ValidationTier.tier3_review,
                            severity=Severity.info,
                            code="MISSING_COMMENT",
                            message=f"{type_name} <{s}> is missing rdfs:comment",
                            subject=str(s),
                        )
                    )
        return findings


class CompetencyQuestionValidator(OntologyValidator):
    """Evaluate whether the ontology can answer a set of competency questions.

    Stub for MVP — future: use LLM to decompose questions into SPARQL,
    check if the ontology's TBox can support each query pattern.
    """

    def validate(self, graph: Graph, **kwargs) -> list[ValidationFinding]:
        """Emit a not-yet-evaluable finding per supplied competency question (MVP stub).

        Args:
            graph: The ontology graph (unused by the current stub).
            **kwargs: Expects ``competency_questions`` (list); returns no findings
                when it is absent or empty.

        Returns:
            An info ``CQ_NOT_EVALUATED`` finding for each competency question.
        """
        questions = kwargs.get("competency_questions", [])
        if not questions:
            return []

        findings = []
        for q in questions:
            findings.append(
                ValidationFinding(
                    validator="competency_questions",
                    tier=ValidationTier.tier3_review,
                    severity=Severity.info,
                    code="CQ_NOT_EVALUATED",
                    message=f"Competency question not yet evaluable: {q}",
                    details={"question": q},
                )
            )
        return findings


class AmbiguousMatchValidator(OntologyValidator):
    """Flag concepts with skos:relatedMatch — ambiguous alignments needing human review."""

    def validate(self, graph: Graph, **kwargs) -> list[ValidationFinding]:
        """Report each ``skos:relatedMatch`` alignment as an ambiguous match for review.

        Reads any attached float-typed confidence annotation to include in the
        finding message.

        Args:
            graph: The ontology graph to scan for ambiguous alignments.
            **kwargs: Unused; accepted for interface compatibility.

        Returns:
            An info finding per ``skos:relatedMatch`` edge; empty when none exist.
        """
        findings = []
        for s, _, o in graph.triples((None, SKOS.relatedMatch, None)):
            if not isinstance(s, URIRef):
                continue
            # Try to read the confidence annotation
            conf = None
            for _, _, v in graph.triples((s, None, None)):
                if isinstance(v, Literal) and v.datatype == XSD.float:
                    conf = float(v)
                    break
            msg = f"Ambiguous match to <{o}>"
            if conf is not None:
                msg += f" (confidence: {conf:.3f})"
            findings.append(
                ValidationFinding(
                    validator="ambiguous_match",
                    tier=ValidationTier.tier3_review,
                    severity=Severity.info,
                    code="AMBIGUOUS_MATCH",
                    message=msg,
                    subject=str(s),
                    details={"candidate": str(o), "confidence": conf},
                )
            )
        return findings
