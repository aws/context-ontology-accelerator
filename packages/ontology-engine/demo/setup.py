#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Setup script for the consolidated Ontology Induction Platform.

Configures the ontology-engine's catalog with foundational ontologies and
their embeddings, so subsequent induction runs can match induced concepts
against known vocabularies (Schema.org, Dublin Core, etc.) instead of
producing all-novel outputs.

What this does (against the consolidated engine on :8001 + data-catalog
on :8003 by default):

  Step 0 — Health check
  Step 1 — Display backend config + verify Bedrock access
  Step 2 — Register foundational ontologies listed in config.yaml:
             POST /ontologies/             (create metadata row)
             POST /ontologies/{id}/fetch   (server downloads the source URL
                                            and parses class/property counts)
  Step 3 — Generate lexical embeddings for classes named in
           `sample_classes` of each config entry (uses Bedrock Titan
           client-side, then POST /embeddings/batch).
  Step 4 — Same for properties named in `sample_properties`.
  Step 5 — Summary.

Config shape (demo/config.yaml, section `foundational_ontologies`):

    - uri: https://schema.org/
      title: Schema.org
      source_url: https://schema.org/version/latest/schemaorg-current-https.ttl
      format: turtle
      domain_tags: [general, web, commerce]
      sample_classes:
        - class_uri: https://schema.org/Thing
          labels: [Thing]
          comments: [The most generic type of item]
        ...
      sample_properties:         # optional
        - property_uri: https://schema.org/name
          labels: [name]
          comments: [The name of the item]
          domain: https://schema.org/Thing
          range: xsd:string

AWS credentials + engine-side env vars (NDB_ENDPOINT, OSS_ENDPOINT,
DYNAMODB_TABLE, etc.) must be set for the engine's own startup, not
setup.py's. setup.py only needs AWS_REGION + Bedrock permissions for
the embedding calls.

Exit codes:
  0  all steps succeeded
  1  health check or Bedrock init failed
  2  one or more ontologies failed to register (partial run)
"""

import os
import sys
import time
from pathlib import Path

import httpx
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm
from rich.table import Table

# ── Config loading ───────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"
console = Console()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        console.print(f"[red]Config not found: {CONFIG_PATH}[/]")
        console.print("  Run: cp demo/config-example.yaml demo/config.yaml")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


_cfg = load_config()
_svc = _cfg.get("services", {})
CATALOG_URL = _svc.get("ontology-catalog", "http://localhost:8001")
ENGINE_URL = _svc.get("ontology-inducer", CATALOG_URL)  # consolidated: same URL

# Bedrock embedding config — read from env (same vars the engine uses)
BEDROCK_REGION = os.getenv("BEDROCK_REGION", os.getenv("AWS_REGION", "us-east-1"))
BEDROCK_EMBED_MODEL_ID = os.getenv("BEDROCK_EMBED_MODEL_ID", "us.cohere.embed-v4:0")
BEDROCK_EMBED_DIMENSIONS = int(os.getenv("BEDROCK_EMBED_DIMENSIONS", "1024"))


# ── Step 0 — Health check ────────────────────────────────────────


def check_health() -> bool:
    console.rule("[bold cyan]Step 0 · Service Health Check")

    urls = {"ontology-engine": CATALOG_URL}
    t = Table(show_header=True)
    t.add_column("Service")
    t.add_column("URL")
    t.add_column("Status")

    all_ok = True
    for name, url in urls.items():
        try:
            r = httpx.get(f"{url}/health", timeout=5)
            ok = r.status_code < 400
            status = "[green]✓[/]" if ok else f"[red]✗ HTTP {r.status_code}[/]"
        except Exception as e:
            ok = False
            status = f"[red]✗ {type(e).__name__}[/]"
        t.add_row(name, url, status)
        if not ok:
            all_ok = False

    console.print(t)
    console.print()
    return all_ok


# ── Step 1 — Backend config display + Bedrock smoke test ─────────


def check_backends() -> bool:
    """Replace the old /backends call with env-var display + a tiny Bedrock
    test (one-token embedding) to confirm IAM and region are set up correctly.
    """
    console.rule("[bold cyan]Step 1 · Embedding Backend Configuration")

    t = Table(show_header=True)
    t.add_column("Variable")
    t.add_column("Value")
    t.add_row("BEDROCK_REGION", BEDROCK_REGION)
    t.add_row("BEDROCK_EMBED_MODEL_ID", BEDROCK_EMBED_MODEL_ID)
    t.add_row("BEDROCK_EMBED_DIMENSIONS", str(BEDROCK_EMBED_DIMENSIONS))
    console.print(t)
    console.print()

    console.print("  Testing Bedrock connectivity with a tiny embedding call…", end=" ")
    try:
        from coa_ontology.bedrock_embeddings import BedrockEmbeddingClient

        client = BedrockEmbeddingClient(
            region=BEDROCK_REGION,
            model_id=BEDROCK_EMBED_MODEL_ID,
            dimensions=BEDROCK_EMBED_DIMENSIONS,
        )
        vec = client._embed_text("connectivity test")
        assert len(vec) == BEDROCK_EMBED_DIMENSIONS, f"Expected {BEDROCK_EMBED_DIMENSIONS} dims, got {len(vec)}"
        console.print("[green]✓[/]")
        console.print()
        return True
    except ImportError:
        console.print("[red]✗[/]")
        console.print("  [red]Engine package not importable. Run `uv sync --all-packages` first.[/]")
        return False
    except Exception as e:
        console.print("[red]✗[/]")
        console.print(f"  [red]Bedrock error: {e}[/]")
        console.print("  [yellow]Check AWS credentials and region.[/]")
        return False


# ── Step 2 — Register foundational ontologies ────────────────────


def _find_existing_ontology(uri: str) -> dict | None:
    """Return the catalog entry for an ontology URI, or None if not registered.

    ``GET /ontologies/`` answers with a camelCase ``{"ontologies": [...]}``
    envelope (its Smithy contract), while ``POST /ontologies/`` answers with a
    snake_case ``OntologyResponse``. Callers use the two interchangeably, so the
    listing row is normalised onto the POST shape for the keys they read.
    """
    resp = httpx.get(f"{CATALOG_URL}/ontologies/", params={"uri": uri}, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("ontologies") or [] if isinstance(payload, dict) else payload
    for row in rows:
        if row.get("uri") == uri:
            return {**row, "ontology_id": row.get("ontologyId") or row.get("ontology_id")}
    return None


def _register_ontology(entry: dict) -> dict | None:
    """POST a new ontology row. Returns the created record, or None on failure.

    If a row with the same URI already exists, we fetch and return it instead
    of re-creating (idempotent).
    """
    existing = _find_existing_ontology(entry["uri"])
    if existing:
        console.print(f"    [yellow]already registered[/] → id={existing['ontology_id']}")
        return existing

    body = {
        "uri": entry["uri"],
        "title": entry.get("title", entry["uri"]),
        "ontology_type": entry.get("ontology_type", "foundational"),
        "description": entry.get("description"),
        "license": entry.get("license"),
        "license_tier": entry.get("license_tier"),
        "source_url": entry.get("source_url"),
        "format": entry.get("format", "turtle"),
        "domain_tags": entry.get("domain_tags", []),
        "imports": entry.get("imports", []),
    }
    try:
        resp = httpx.post(f"{CATALOG_URL}/ontologies/", json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        console.print(f"    [red]POST /ontologies/ failed: {e}[/]")
        return None


def _server_side_fetch(
    ontology_id: str,
    source_url: str,
    extract_properties_for_classes: list[str] | None = None,
    known_ontology_uris: list[str] | None = None,
    extract_all_properties: bool = False,
) -> dict | None:
    """Have the engine download + parse the ontology from its source URL.

    URL-encodes the ontology_id because some URIs contain '#' (e.g.
    PROV-O's http://www.w3.org/ns/prov#) which HTTP clients treat as a
    fragment boundary unless escaped.

    When ``extract_properties_for_classes`` is provided, the engine returns
    every property whose rdfs:domain or rdfs:range (or schema:domainIncludes/
    rangeIncludes) intersects that class list. We pass setup.py's curated
    sample_classes so Step 4 can embed the auto-extracted properties
    alongside any hand-listed ones from config.

    When ``extract_all_properties=True``, the engine returns EVERY property
    declared in the ontology regardless of domain/range. This overrides
    the class-based filter.

    When ``known_ontology_uris`` is provided, the engine flags external
    namespaces referenced via rdfs:subClassOf/domain/range that aren't
    covered by any loaded ontology — so we can warn the user that e.g.
    their customer ontology references hpclab:* which isn't loaded.
    """
    from urllib.parse import quote

    encoded_id = quote(ontology_id, safe="")
    body: dict = {"source_url": source_url}
    if extract_properties_for_classes:
        body["extract_properties_for_classes"] = extract_properties_for_classes
    if extract_all_properties:
        body["extract_all_properties"] = True
    if known_ontology_uris:
        body["known_ontology_uris"] = known_ontology_uris
    try:
        resp = httpx.post(
            f"{CATALOG_URL}/ontologies/{encoded_id}/fetch",
            json=body,
            timeout=600,  # large ontologies like Schema.org (~17MB) can take a while
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        console.print(f"    [yellow]server-side fetch failed (non-fatal): {e}[/]")
        return None


def load_ontologies(config: dict) -> list[dict]:
    """Register every grounding ontology in config.yaml — foundational AND customer-provided.

    Both tiers go through the same registration + embedding pipeline. The
    difference is metadata (ontology_type + the tier tag on each returned
    registered entry), which downstream steps use to tell the customer how
    much of their schema grounded to their own ontology vs cross-industry
    foundations.

    Config shape:
      foundational_ontologies: [...]   # cross-industry standards
      customer_ontologies:     [...]   # the customer's own domain ontology (optional)

    Returns the list of registered entries (both tiers mixed), each annotated
    with a 'tier' key ('foundational' or 'customer'). Order: foundational
    first, then customer — so demo.py Step 2 displays them grouped.
    """
    console.rule("[bold cyan]Step 2 · Register Grounding Ontologies")

    foundational = config.get("foundational_ontologies", []) or []
    customer = config.get("customer_ontologies", []) or []
    all_entries = [(e, "foundational") for e in foundational] + [(e, "customer") for e in customer]

    if not all_entries:
        console.print("  [yellow]No grounding ontologies configured.[/]")
        console.print("  Edit demo/config.yaml — add foundational_ontologies or customer_ontologies.")
        console.print()
        return []

    console.print(
        f"  Loading [bold cyan]{len(foundational)}[/] foundational + "
        f"[bold magenta]{len(customer)}[/] customer-provided ontologies."
    )
    console.print()

    # Known-URI set for unresolved-reference detection — the engine needs to
    # know which namespaces ARE loaded so it can flag references to anything
    # else. We include every ontology URI in either tier plus the per-entry
    # base URI (strip fragment/trailing slash).
    known_uris = []
    for entry, _ in all_entries:
        uri = entry.get("uri", "")
        if uri:
            known_uris.append(uri)
            # Also add the namespace without trailing # or /
            known_uris.append(uri.rstrip("/#"))

    registered = []
    unresolved_by_ontology: dict[str, list[dict]] = {}

    current_tier = None
    for entry, tier in all_entries:
        # Print a tier header when the tier changes
        if tier != current_tier:
            current_tier = tier
            label = {
                "foundational": "FOUNDATIONAL TIER (cross-industry)",
                "customer": "CUSTOMER TIER (domain-specific)",
            }[tier]
            color = {"foundational": "cyan", "customer": "magenta"}[tier]
            console.print(f"  [bold {color}]── {label} ──[/]")

        title = entry.get("title", entry["uri"])
        console.print(f"    [bold]{title}[/] ({entry['uri']})")

        record = _register_ontology(entry)
        if not record:
            continue

        # Server-side fetch — now also asks the engine to flag unresolved refs
        source_url = entry.get("source_url")
        extracted_properties: list[dict] = []
        unresolved: list[dict] = []
        if source_url:
            class_uris = [c["class_uri"] for c in entry.get("sample_classes", []) if c.get("class_uri")]
            # Tier-dependent default: customer ontologies default to
            # extract-all (a customer who brings their own ontology typically
            # wants the pipeline to use the WHOLE thing without hand-picking
            # which properties matter — that's the customer's ontology
            # declaring what matters). Foundational ontologies keep filter-by-
            # sample_classes so a 1700-property Schema.org doesn't balloon
            # the Bedrock embedding bill when we only curated 16 relevant classes.
            default_extract_all = tier == "customer"
            extract_all = bool(entry.get("extract_all_properties", default_extract_all))
            fetch_result = _server_side_fetch(
                record["ontology_id"],
                source_url,
                extract_properties_for_classes=class_uris or None,
                known_ontology_uris=known_uris,
                extract_all_properties=extract_all,
            )
            if fetch_result:
                cc = fetch_result.get("class_count")
                pc = fetch_result.get("property_count")
                extras = []
                if cc is not None:
                    extras.append(f"{cc} classes")
                if pc is not None:
                    extras.append(f"{pc} properties")
                if extras:
                    console.print(f"      server-side parse: {', '.join(extras)}")
                extracted_properties = fetch_result.get("properties") or []
                if extracted_properties:
                    console.print(
                        f"      auto-extracted {len(extracted_properties)} properties "
                        f"for {len(class_uris)} sample classes"
                    )
                unresolved = fetch_result.get("unresolved_references") or []
                if unresolved:
                    unresolved_by_ontology[title] = unresolved

        registered.append(
            {
                "record": record,
                "config": entry,
                "tier": tier,
                "extracted_properties": extracted_properties,
            }
        )

    console.print()
    # Tier-grouped summary
    by_tier = {"foundational": 0, "customer": 0}
    for item in registered:
        by_tier[item["tier"]] += 1
    console.print(
        f"  Registered [bold cyan]{by_tier['foundational']}[/] foundational + "
        f"[bold magenta]{by_tier['customer']}[/] customer-provided = "
        f"[bold]{len(registered)}[/] total."
    )

    # ── Unresolved-reference warnings ─────────────────────────────
    if unresolved_by_ontology:
        console.print()
        console.print("  [yellow]⚠ Dangling-reference warnings:[/]")
        for onto_title, refs in unresolved_by_ontology.items():
            console.print(
                f"    [bold]{onto_title}[/] references external namespaces not covered by any loaded ontology:"
            )
            for ref in refs:
                sample = ", ".join(ref.get("sample_iris", [])[:3])
                console.print(
                    f"      [dim]•[/] [yellow]{ref['namespace']}[/] ({ref['reference_count']} refs; e.g. {sample})"
                )
        console.print(
            "  [dim]    Grounding to classes in these namespaces will not work until "
            "the referenced ontologies are loaded. Add them as "
            "foundational_ontologies or customer_ontologies entries in config.yaml, "
            "or ignore if the references are intentional.[/]"
        )

    console.print()
    return registered


# ── Step 3+4 — Lexical embeddings for sample classes and properties ──


def _build_class_text(entry: dict) -> str:
    """Concatenate labels + comments into a single string for embedding."""
    labels = " ".join(entry.get("labels", []))
    comments = " ".join(entry.get("comments", []))
    return f"{labels} {comments}".strip()


def _build_property_text(entry: dict) -> str:
    labels = " ".join(entry.get("labels", []))
    comments = " ".join(entry.get("comments", []))
    domain = entry.get("domain", "")
    rnge = entry.get("range", "")
    context = f"domain={domain} range={rnge}" if (domain or rnge) else ""
    return " ".join(filter(None, [labels, comments, context])).strip()


def _embed_and_post(
    ontology_id: str,
    entity_type: str,
    entries: list[dict],
    uri_key: str,
    text_fn,
    bedrock_client,
) -> int:
    """Shared worker for class and property embedding.

    Uses a ThreadPoolExecutor to parallelize Bedrock calls (~10x faster for
    a batch of 700+). Each call is I/O-bound (HTTP round-trip to Bedrock
    us-east-1), so threads give near-linear speedup up to the Bedrock
    per-account concurrency limit. Individual call failures are logged
    and skipped — one bad entry doesn't abort the batch.

    Returns the number of embeddings successfully stored.
    """
    if not entries:
        return 0

    # Label uses the right English plural (classes, properties), not naive +"s"
    plural = {"class": "classes", "property": "properties"}.get(entity_type, f"{entity_type}s")

    # Precompute texts; skip entries whose text_fn returns empty
    jobs: list[tuple[dict, str]] = []  # (entry, text)
    for entry in entries:
        text = text_fn(entry)
        if text:
            jobs.append((entry, text))

    if not jobs:
        return 0

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Cap at Bedrock's practical throughput — Titan embeddings have per-account
    # TPM limits but the per-request latency is ~150-200ms so going higher than
    # ~50 threads stops helping and starts causing ThrottlingException retries.
    # 40 is aggressive but holds within typical account quotas; drop to 20 if
    # you see ThrottlingException in the yellow warnings below.
    max_workers = 40
    batch: list[dict] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task(f"  Embedding {plural}…", total=len(jobs))

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_entry = {pool.submit(bedrock_client._embed_text, text): entry for entry, text in jobs}
            for future in as_completed(future_to_entry):
                entry = future_to_entry[future]
                try:
                    vector = future.result()
                except Exception as e:
                    console.print(f"    [yellow]skip {entry.get(uri_key)}: bedrock error {e}[/]")
                    progress.update(task, advance=1)
                    continue

                batch.append(
                    {
                        "ontology_id": ontology_id,
                        "entity_uri": entry[uri_key],
                        "entity_type": entity_type,
                        "embedding_type": "lexical",
                        "model_id": bedrock_client.model_id,
                        "vector": vector,
                        "dimensions": bedrock_client.dimensions,
                    }
                )
                progress.update(task, advance=1)

    if not batch:
        return 0

    try:
        resp = httpx.post(
            f"{CATALOG_URL}/embeddings/batch",
            json={"embeddings": batch},
            timeout=60,
        )
        resp.raise_for_status()
        stored = len(resp.json())
    except httpx.HTTPError as e:
        console.print(f"    [red]POST /embeddings/batch failed: {e}[/]")
        stored = 0

    return stored


def generate_embeddings(registered: list[dict]):
    console.rule("[bold cyan]Step 3 · Generate Class Embeddings")

    if not registered:
        console.print("  [yellow]No ontologies to embed. Skipping.[/]")
        console.print()
        return

    from coa_ontology.bedrock_embeddings import BedrockEmbeddingClient

    bedrock = BedrockEmbeddingClient(
        region=BEDROCK_REGION,
        model_id=BEDROCK_EMBED_MODEL_ID,
        dimensions=BEDROCK_EMBED_DIMENSIONS,
    )

    total = 0
    for item in registered:
        record = item["record"]
        cfg = item["config"]
        sample_classes = cfg.get("sample_classes", [])
        console.print(f"  [bold]{cfg.get('title', record['uri'])}[/] — {len(sample_classes)} sample classes")
        n = _embed_and_post(
            ontology_id=record["ontology_id"],
            entity_type="class",
            entries=sample_classes,
            uri_key="class_uri",
            text_fn=_build_class_text,
            bedrock_client=bedrock,
        )
        total += n

    console.print()
    console.print(f"  Stored [bold]{total}[/] class embeddings.")
    console.print()


def generate_property_embeddings(registered: list[dict]):
    console.rule("[bold cyan]Step 4 · Generate Property Embeddings")

    if not registered:
        console.print("  [yellow]No ontologies to embed. Skipping.[/]")
        console.print()
        return

    from coa_ontology.bedrock_embeddings import BedrockEmbeddingClient

    bedrock = BedrockEmbeddingClient(
        region=BEDROCK_REGION,
        model_id=BEDROCK_EMBED_MODEL_ID,
        dimensions=BEDROCK_EMBED_DIMENSIONS,
    )

    total = 0
    for item in registered:
        record = item["record"]
        cfg = item["config"]
        curated = cfg.get("sample_properties", [])
        auto_extracted = item.get("extracted_properties", []) or []

        # Merge: curated wins over auto-extracted for the same URI, so
        # hand-written entries in config.yaml can override an auto-extract.
        by_uri: dict[str, dict] = {}
        for ap in auto_extracted:
            uri = ap.get("property_uri")
            if uri:
                # The pydantic response shapes domain/range as lists.
                # Flatten list[str] → "first match" for the embedding-text
                # helper (_build_property_text expects string, not list).
                by_uri[uri] = {
                    "property_uri": uri,
                    "labels": ap.get("labels", []),
                    "comments": ap.get("comments", []),
                    "domain": (ap.get("domain") or [None])[0],
                    "range": (ap.get("range") or [None])[0],
                }
        for cp in curated:
            uri = cp.get("property_uri")
            if uri:
                by_uri[uri] = cp  # curated overrides

        all_props = list(by_uri.values())
        title = cfg.get("title", record["uri"])
        console.print(
            f"  [bold]{title}[/] — {len(all_props)} properties "
            f"(curated={len(curated)}, auto-extracted={len(auto_extracted)})"
        )
        n = _embed_and_post(
            ontology_id=record["ontology_id"],
            entity_type="property",
            entries=all_props,
            uri_key="property_uri",
            text_fn=_build_property_text,
            bedrock_client=bedrock,
        )
        total += n

    console.print()
    console.print(f"  Stored [bold]{total}[/] property embeddings.")
    console.print()


# ── Step 5 — Structural embeddings (not yet supported) ───────────


# ── main ─────────────────────────────────────────────────────────


def main():
    console.print(
        Panel.fit(
            "[bold]Ontology Induction Platform[/] — Setup",
            subtitle="Ctrl+C to exit at any time",
        )
    )
    console.print()

    t0 = time.perf_counter()

    if not check_health():
        console.print("[red]One or more services are unhealthy. Start them and retry.[/]")
        sys.exit(1)

    if not check_backends():
        console.print("[red]Bedrock check failed. Fix credentials/region and retry.[/]")
        sys.exit(1)

    config = load_config()

    if not Confirm.ask("Proceed with foundational ontology registration?", default=True):
        console.print("Aborted.")
        sys.exit(0)

    registered = load_ontologies(config)
    generate_embeddings(registered)
    generate_property_embeddings(registered)

    elapsed = time.perf_counter() - t0
    console.rule("[bold green]Setup Complete")
    console.print(f"  Total wall-clock: [bold]{elapsed:.1f}s[/]")
    console.print(f"  Registered ontologies: [bold]{len(registered)}[/]")
    console.print()
    console.print("Now run [bold]python demo/demo.py[/] to exercise induction with grounding.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
