#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Interactive demo for the Ontology Induction Platform."""

import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.spinner import Spinner
from rich.table import Table

console = Console()

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        console.print(f"[red]Config not found: {CONFIG_PATH}[/]")
        console.print("  cp config-example.yaml config.yaml")
        sys.exit(1)
    return yaml.safe_load(CONFIG_PATH.read_text())


_cfg = _load_config()
_svc = _cfg.get("services", {})
_defaults = _cfg.get("induction_defaults", {})

CATALOG_URL = _svc.get("ontology-catalog", "http://localhost:8001")
DATA_CATALOG_URL = _svc.get("data-catalog", "http://localhost:8003")
INDUCER_URL = _svc.get("ontology-inducer", "http://localhost:8004")
VALIDATION_URL = _svc.get("ontology-validation", "http://localhost:8005")

SERVICES = {
    "ontology-catalog": CATALOG_URL,
    "embedding-generator": _svc.get("embedding-generator", "http://localhost:8002"),
    "data-catalog": DATA_CATALOG_URL,
    "ontology-inducer": INDUCER_URL,
    "ontology-validation": VALIDATION_URL,
}

timings: dict[str, float] = {}


def _prompt_selection(label: str, max_index: int, allow_none: bool = False) -> list[int]:
    """Prompt the user for 'all' | 'none' | '1,3,5' and re-prompt on bad input.

    Returns 0-indexed integer indices. 'all' returns 0..max_index-1. 'none'
    returns [] (only when allow_none=True). Out-of-range numbers are skipped
    with a warning; non-integer tokens re-prompt.
    """
    options = "all" + (" | none" if allow_none else "")
    while True:
        raw = Prompt.ask(label, default="all").strip().lower()
        if raw == "all":
            return list(range(max_index))
        if raw == "none" and allow_none:
            return []
        try:
            parsed = [int(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError:
            console.print(f"  [yellow]Couldn't parse {raw!r} as numbers. Try e.g. '1,3,5' or '{options}'.[/]")
            continue
        indices = [i - 1 for i in parsed if 1 <= i <= max_index]
        dropped = [i for i in parsed if i < 1 or i > max_index]
        if dropped:
            console.print(f"  [yellow]Ignored out-of-range: {dropped} (valid: 1–{max_index})[/]")
        if not indices:
            console.print(f"  [yellow]No valid selections. Try e.g. '1,3,5' or '{options}'.[/]")
            continue
        return indices


def _http_fetch(method: str, url: str, **kwargs) -> dict | list:
    """httpx GET/POST with friendly error messages and exit on failure.

    Intended for primary user-facing calls. Polling loops that already
    handle their own retry/timeout should use httpx directly.
    """
    try:
        resp = httpx.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except httpx.TimeoutException:
        console.print(f"  [red]Timeout calling {method} {url}[/]")
    except httpx.ConnectError:
        console.print(f"  [red]Couldn't connect to {url}. Is the service running?[/]")
    except httpx.HTTPStatusError as e:
        console.print(f"  [red]HTTP {e.response.status_code} from {method} {url}[/]")
        console.print(f"  [dim]{e.response.text[:200]}[/]")
    except httpx.HTTPError as e:
        console.print(f"  [red]Network error calling {method} {url}: {e}[/]")
    except json.JSONDecodeError as e:
        console.print(f"  [red]Invalid JSON from {method} {url}: {e}[/]")
    sys.exit(1)


def _download_ontology_turtle(encoded_id: str, timeout: int = 30) -> str:
    """GET /ontologies/{id}/download and return the ontology Turtle.

    ``/download`` no longer inlines the Turtle: it returns
    ``{"downloadUrl", "expiresInSeconds"}`` pointing at a presigned S3 object,
    because a 6+ MB ontology would exceed the API Gateway / Lambda response
    limit (#143). Follow that URL out-of-band. A raw-body fallback keeps the demo
    working against an ontology-engine that still inlines.

    Raises ``httpx.HTTPError`` (incl. ``HTTPStatusError``) like the direct call it
    replaces, so existing call-site error handling still applies.
    """
    resp = httpx.get(f"{CATALOG_URL}/ontologies/{encoded_id}/download", timeout=timeout)
    resp.raise_for_status()
    download_url = None
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        download_url = payload.get("downloadUrl") or payload.get("download_url")
    if not download_url:
        return resp.text
    obj = httpx.get(download_url, timeout=timeout)
    obj.raise_for_status()
    return obj.text


def _list_ontologies(**params) -> list[dict]:
    """GET /ontologies/ and return the rows, unwrapping the response envelope.

    ListOntologies answers with camelCase ``{"ontologies": [...]}`` per its
    Smithy contract. A bare array is still accepted so the demo keeps working
    against an older deployment.
    """
    payload = _http_fetch("GET", f"{CATALOG_URL}/ontologies/", params=params, timeout=10)
    if isinstance(payload, dict):
        return payload.get("ontologies") or []
    return payload or []


def timed(label: str):
    """Context manager that records wall-clock time for a step."""

    class _Timer:
        def __enter__(self):
            self.start = time.perf_counter()
            return self

        def __exit__(self, *_):
            self.elapsed = time.perf_counter() - self.start
            timings[label] = self.elapsed

    return _Timer()


# ── Step 0: Health ──────────────────────────────────────────────


def check_health():
    console.rule("[bold cyan]Step 0 · Service Health Check")

    for attempt in range(12):
        all_ok = True
        probes = {}
        for name, url in SERVICES.items():
            try:
                start = time.perf_counter()
                r = httpx.get(f"{url}/health", timeout=5)
                ms = (time.perf_counter() - start) * 1000
                ok = r.status_code < 400
                if ok and r.json().get("neptune_analytics") == "unreachable":
                    ok = False
                probes[name] = (ok, "[green]✓[/]" if ok else "[red]✗ NA unreachable[/]", f"{ms:.0f}ms")
            except Exception:
                probes[name] = (False, "[red]✗ unreachable[/]", "—")
            if not probes[name][0]:
                all_ok = False
        if all_ok:
            break
        if attempt == 0:
            console.print("  [yellow]Waiting for services…[/]", end="")
        console.print(".", end="")
        time.sleep(5)
    else:
        console.print()
        console.print("[red]Services did not become healthy after 60s. Run: docker compose up -d[/]")
        sys.exit(1)

    if attempt > 0:
        console.print()

    t = Table(show_header=True)
    t.add_column("Service")
    t.add_column("URL")
    t.add_column("Status")
    t.add_column("Latency")
    for name, url in SERVICES.items():
        _, status, latency = probes[name]
        t.add_row(name, url, status, latency)
    console.print(t)
    console.print()


# ── Step 1: Browse data catalog ────────────────────────────────


def browse_data_catalog() -> list[str]:
    """Browse datasources and let the user pick one or more.

    Returns the list of ``datasourceId`` strings to pass to the inducer.
    The consolidated induction API (POST /induce/) accepts datasource IDs
    and fetches the tables under each — we no longer pick individual tables.
    """
    console.rule("[bold cyan]Step 1 · Data Catalog — Available Datasources")

    with timed("fetch_catalogs"):
        catalogs = _http_fetch(
            "GET",
            f"{DATA_CATALOG_URL}/api/v1/catalogs/",
            timeout=10,
            follow_redirects=True,
        )

    # Flatten namespaces × sources so each row is a selectable datasource
    rows: list[tuple[str, str, str, int]] = []  # (datasourceId, namespaceId, database, tableCount)
    for ns in catalogs:
        nid = ns.get("namespaceId", "—")
        for src in ns.get("sources", []):
            sid = src.get("datasourceId", "—")
            for db in src.get("databases", []):
                tcount = len(db.get("tables", []))
                rows.append((sid, nid, db.get("name", "—"), tcount))

    if not rows:
        console.print("  [red]No datasources available in data-catalog.[/]")
        console.print("  Did you run [bold]python demo/stage_fixtures.py[/] and restart the catalog service?")
        sys.exit(1)

    t = Table(show_header=True, title="Datasources")
    t.add_column("#", style="bold")
    t.add_column("Datasource ID")
    t.add_column("Namespace")
    t.add_column("Database")
    t.add_column("Tables", justify="right")

    for i, (sid, nid, db, tcount) in enumerate(rows, 1):
        t.add_row(str(i), sid, nid, db, str(tcount))

    console.print(t)
    console.print()

    selection_prompt = "Select datasources to induct (comma-separated numbers, or [bold]all[/])"
    indices = _prompt_selection(selection_prompt, len(rows))
    chosen = [rows[i] for i in indices]

    ids = [sid for sid, _, _, _ in chosen]
    total_tables = sum(tcount for _, _, _, tcount in chosen)
    console.print(f"  Selected [bold]{len(ids)}[/] datasource(s) ({total_tables} tables): {', '.join(ids)}")
    console.print()
    return ids


# ── Step 2: Browse ontology catalog ────────────────────────────


def browse_ontology_catalog() -> list[str] | None:
    console.rule("[bold cyan]Step 2 · Ontology Catalog — Grounding Ontologies")

    # Fetch both foundational and customer-provided in one pass, merge.
    with timed("fetch_ontologies"):
        foundational = _list_ontologies(ontology_type="foundational")
        customer = _list_ontologies(ontology_type="customer_provided")

    # Tag with tier (engine returns ontology_type per record but we want a
    # short display label for the table).
    for o in foundational:
        o["_tier_display"] = "foundational"
    for o in customer:
        o["_tier_display"] = "customer"
    onts = foundational + customer

    if not onts:
        console.print("  [yellow]No grounding ontologies loaded yet.[/]")
        console.print("  Induction will create all concepts as [bold]novel[/].")
        console.print()
        return None

    t = Table(show_header=True, title="Grounding Ontologies")
    t.add_column("#", style="bold")
    t.add_column("Tier")
    t.add_column("Title")
    t.add_column("URI")
    t.add_column("Classes", justify="right")
    t.add_column("Tags")

    for i, o in enumerate(onts, 1):
        tier = o.get("_tier_display", "?")
        tier_color = {"foundational": "cyan", "customer": "magenta"}.get(tier, "white")
        t.add_row(
            str(i),
            f"[{tier_color}]{tier}[/]",
            o["title"],
            o["uri"],
            str(o.get("classCount") or "—"),
            ", ".join(o.get("domainTags") or []) or "—",
        )

    console.print(t)
    # Tier summary line
    n_f = sum(1 for o in onts if o["_tier_display"] == "foundational")
    n_c = sum(1 for o in onts if o["_tier_display"] == "customer")
    console.print(
        f"  [dim]{n_f} foundational + {n_c} customer-provided. "
        f"Customer-tier matches are higher-value — they indicate your induced concepts "
        f"align with your own domain model.[/]"
    )
    console.print()

    selection = (
        Prompt.ask(
            "Ground against which ontologies? (comma-separated numbers, [bold]all[/], or [bold]none[/])",
            default="all",
        )
        .strip()
        .lower()
    )

    if selection == "none":
        return []
    if selection == "all":
        return None  # None = no restriction

    # Parse comma-separated indices with error handling
    while True:
        try:
            indices = [int(x.strip()) - 1 for x in selection.split(",") if x.strip()]
            break
        except ValueError:
            console.print(f"  [yellow]Couldn't parse {selection!r} as numbers. Try e.g. '1,3,5' or 'all' or 'none'.[/]")
            selection = (
                Prompt.ask(
                    "Ground against which ontologies? (comma-separated numbers, [bold]all[/], or [bold]none[/])",
                    default="all",
                )
                .strip()
                .lower()
            )
            if selection == "none":
                return []
            if selection == "all":
                return None
    return [onts[i]["ontologyId"] for i in indices if 0 <= i < len(onts)]


# ── Step 3: Configure & run induction ──────────────────────────


def run_induction(datasource_ids: list[str], grounding_ids: list[str] | None) -> dict:
    console.rule("[bold cyan]Step 3 · Ontology Induction")

    # Prompt repeatedly until we get a well-formed absolute URI. An earlier
    # demo run had a user type 'abdellah/test' which is a relative URI —
    # the accept handler tried to SPARQL-UPDATE with it and threw 500. Validate
    # here so the user sees the error immediately rather than 5 minutes later
    # after induction completes.
    def _looks_like_absolute_uri(s: str) -> bool:
        return s.startswith(("http://", "https://", "urn:")) and len(s) > len("https://") + 2

    default_uri = _defaults.get("ontology_uri_prefix", "https://example.com/ontology/retail#")
    while True:
        uri_prefix = Prompt.ask("Ontology URI prefix", default=default_uri)
        if _looks_like_absolute_uri(uri_prefix):
            break
        console.print(
            f"  [yellow]{uri_prefix!r} isn't a valid absolute URI.[/] "
            "Must start with http://, https://, or urn:. "
            "Example: [bold]https://example.com/ontology/insurance#[/]"
        )

    label = Prompt.ask("Ontology label", default=_defaults.get("label", "Retail Domain Ontology"))
    threshold = float(Prompt.ask("Confidence threshold", default=str(_defaults.get("confidence_threshold", 0.80))))

    # Induction strategy — selects which pipeline the server runs.
    # Distinct from scoring_strategy (which tunes the matcher within a pipeline).
    _VALID_STRATEGIES = ("table_to_ontology", "rigor_ontology")
    default_induction_strategy = _defaults.get("strategy", "table_to_ontology")
    while True:
        induction_strategy = Prompt.ask(
            "Induction strategy ([bold]table_to_ontology[/] | [bold]rigor_ontology[/])",
            default=default_induction_strategy,
        ).strip()
        if induction_strategy in _VALID_STRATEGIES:
            break
        console.print(f"  [yellow]Unknown strategy {induction_strategy!r}. Valid: {', '.join(_VALID_STRATEGIES)}.[/]")

    default_scoring_strategy = _defaults.get("scoring_strategy", "lexical")
    scoring_strategy = Prompt.ask(
        "Scoring strategy ([bold]lexical[/] | [bold]structural_fusion[/])",
        default=default_scoring_strategy,
    )
    structural_weight = _defaults.get("structural_weight", 0.05)
    if scoring_strategy == "structural_fusion":
        structural_weight = float(Prompt.ask("Structural weight (0-1)", default=str(structural_weight)))

    namespace = Prompt.ask(
        "Namespace (for multi-tenant isolation)",
        default=_defaults.get("namespace", "default"),
    )

    payload = {
        "datasource_ids": datasource_ids,
        "ontology_uri_prefix": uri_prefix,
        "namespace": namespace,
        "label": label,
        "confidence_threshold": threshold,
        "strategy": induction_strategy,
        "scoring_strategy": scoring_strategy,
        "structural_weight": structural_weight,
    }
    if grounding_ids is not None:
        payload["grounding_ontology_ids"] = grounding_ids

    console.print()
    console.print(f"  Submitting induction job for [bold]{len(datasource_ids)}[/] datasource(s)…")

    with timed("induction_submit"):
        job = _http_fetch("POST", f"{INDUCER_URL}/induce/", json=payload, timeout=30)

    job_id = job["job_id"]
    console.print(f"  Job ID: [bold]{job_id}[/]")
    console.print()

    # Poll with live status
    prev_status = None
    phase_start = time.perf_counter()
    induction_start = time.perf_counter()

    with Live(Spinner("dots", text="Waiting…"), console=console, refresh_per_second=4) as live:
        while True:
            r = httpx.get(f"{INDUCER_URL}/induce/jobs/{job_id}", timeout=10).json()
            status = r["status"]

            if status != prev_status:
                if prev_status:
                    elapsed = time.perf_counter() - phase_start
                    timings[f"induction.{prev_status}"] = elapsed
                phase_start = time.perf_counter()
                prev_status = status
                live.update(Spinner("dots", text=f"  Status: [bold yellow]{status}[/]"))

            if status == "completed":
                timings["induction_total"] = time.perf_counter() - induction_start
                break
            if status == "failed":
                timings["induction_total"] = time.perf_counter() - induction_start
                console.print(f"  [red]Job failed: {r.get('error')}[/]")
                sys.exit(1)

            time.sleep(0.5)

    console.print(f"  [green]✓ Induction completed in {timings['induction_total']:.2f}s[/]")
    console.print()
    return r


# ── Step 4: Show results ───────────────────────────────────────


def show_results(job: dict):
    console.rule("[bold cyan]Step 4 · Grounding Results")

    report = job.get("report")
    if not report:
        console.print("  [yellow]No report available.[/]")
        return

    matches = report.get("matches", [])

    t = Table(show_header=True, title="Concept Matches")
    t.add_column("Source Table")
    t.add_column("Source Column")
    t.add_column("Matched To")
    t.add_column("Score", justify="right")
    t.add_column("Type")

    type_colors = {
        "exact": "green",
        "high_confidence": "cyan",
        "ambiguous": "yellow",
        "novel": "red",
    }

    for m in matches:
        matched = m.get("matched_class_uri") or "—"
        score = f"{m['similarity']:.3f}" if m.get("similarity") else "—"
        mtype = m.get("match_type", "?")
        color = type_colors.get(mtype, "white")
        t.add_row(m["source_table"], m["source_column"], matched, score, f"[{color}]{mtype}[/]")

    console.print(t)
    console.print()

    # Show candidates for column matches if available
    has_candidates = any(m.get("candidates") for m in matches if m.get("source_column"))
    if has_candidates and Confirm.ask("Show top candidates per column?", default=False):
        for m in matches:
            if not m.get("source_column") or not m.get("candidates"):
                continue
            ct = Table(
                show_header=True,
                title=f"{m['source_table']}.{m['source_column']} — strategy: {m.get('scoring_strategy', '?')}",
            )
            ct.add_column("#", style="bold")
            ct.add_column("Candidate")
            ct.add_column("Lexical", justify="right")
            ct.add_column("Structural", justify="right")
            ct.add_column("Fused", justify="right")
            for j, c in enumerate(m["candidates"][:5]):
                is_winner = c["entity_uri"] == m.get("matched_class_uri")
                prefix = "[bold green]►[/] " if is_winner else "  "
                ss = f"{c['structural_sim']:.3f}" if c.get("structural_sim") is not None else "—"
                fused = f"{c['fused_score']:.3f}" if c.get("fused_score") is not None else "—"
                ct.add_row(
                    str(j + 1),
                    f"{prefix}{c['entity_uri']}",
                    f"{c['lexical_sim']:.3f}",
                    ss,
                    fused,
                )
            console.print(ct)
            console.print()
    console.print()

    # Summary counts — tier-aware.
    # Classify each match by the tier of the ontology it grounded to.
    by_type = {}
    for m in matches:
        by_type[m["match_type"]] = by_type.get(m["match_type"], 0) + 1

    # Fetch ontology_type for each matched_ontology_id so we can split
    # "grounded" into "grounded_customer" vs "grounded_foundational".
    # Cache by id to avoid N round trips.
    ontology_type_cache: dict[str, str] = {}

    def _tier_for(onto_id: str | None) -> str | None:
        if not onto_id:
            return None
        if onto_id in ontology_type_cache:
            return ontology_type_cache[onto_id]
        try:
            resp = httpx.get(f"{CATALOG_URL}/ontologies/{quote(onto_id, safe='')}", timeout=5)
            if resp.status_code == 200:
                t = resp.json().get("ontology_type") or ""
                ontology_type_cache[onto_id] = t
                return t
        except httpx.HTTPError:
            pass
        ontology_type_cache[onto_id] = ""
        return ""

    grounded_customer = 0
    grounded_foundational = 0
    grounded_previously_induced = 0  # matches against past accepted induced ontologies
    for m in matches:
        if m.get("match_type") == "novel":
            continue
        onto_id = m.get("matched_ontology_id")
        t = _tier_for(onto_id) or ""
        if t == "customer_provided":
            grounded_customer += 1
        elif t == "foundational":
            grounded_foundational += 1
        elif t == "induced":
            grounded_previously_induced += 1

    total = len(matches)
    pct_customer = (grounded_customer / total * 100) if total else 0
    pct_foundational = (grounded_foundational / total * 100) if total else 0
    pct_previously_induced = (grounded_previously_induced / total * 100) if total else 0
    pct_novel = (by_type.get("novel", 0) / total * 100) if total else 0

    summary = f"  Tables: [bold]{report['tables_processed']}[/]  Columns: [bold]{report['columns_processed']}[/]"
    console.print(summary)
    console.print(
        f"  [bold magenta]Grounded to customer ontology:[/]   "
        f"[bold]{grounded_customer}/{total}[/] ({pct_customer:.0f}%)  "
        f"[dim]← high-value, aligned to your domain model[/]"
    )
    console.print(
        f"  [bold cyan]Grounded to foundational:[/]        "
        f"[bold]{grounded_foundational}/{total}[/] ({pct_foundational:.0f}%)  "
        f"[dim]← generic cross-industry concepts[/]"
    )
    if grounded_previously_induced:
        console.print(
            f"  [bold blue]Grounded to previously-induced:[/]  "
            f"[bold]{grounded_previously_induced}/{total}[/] ({pct_previously_induced:.0f}%)  "
            f"[dim]← matches to ontologies YOU accepted earlier (self-match loop?)[/]"
        )
    console.print(
        f"  [bold red]Novel (needs review):[/]             "
        f"[bold]{by_type.get('novel', 0)}/{total}[/] ({pct_novel:.0f}%)  "
        f"[dim]← no existing ontology covered this[/]"
    )
    if by_type.get("ambiguous"):
        console.print(
            f"  [bold yellow]Ambiguous:[/]                        "
            f"[bold]{by_type.get('ambiguous', 0)}/{total}[/]  "
            f"[dim]← multiple candidates close in score[/]"
        )

    if report.get("grounding_ontologies_used"):
        console.print(f"  Grounding sources: {', '.join(report['grounding_ontologies_used'])}")

    if report.get("induced_ontology_id"):
        console.print(f"  Stored as ontology ID: [bold]{report['induced_ontology_id']}[/]")

    # Expose tier counts on the report for downstream use (benchmark scorer, etc.)
    report["_grounded_customer"] = grounded_customer
    report["_grounded_foundational"] = grounded_foundational

    console.print()
    return report


# ── Step 4b: Benchmark against golden ontology ─────────────────

# Known golden-ontology URLs per datasource. If demo.py detects one
# of these datasources was selected in Step 1, it fetches the URL
# automatically and scores against it. Other datasources get an
# interactive prompt (path, URL, or skip).
_KNOWN_GOLDEN_URLS = {
    "ds-pc-insurance": (
        "ACME Insurance (Sequeda/Allemang/Jacob 2023 benchmark)",
        "https://raw.githubusercontent.com/datadotworld/cwd-benchmark-data/main/ACME_Insurance/ontology/insurance.ttl",
    ),
}


def benchmark_against_golden(report: dict, datasource_ids: list[str]):
    """Compare the induced ontology to a golden reference and render a scorecard.

    Called between show_results() and run_validation(). If any selected
    datasource has a registered golden URL, fetches it automatically.
    Otherwise, prompts the user for a local path, URL, or skip.
    """
    console.rule("[bold cyan]Step 4b · Benchmark Against Golden Ontology")

    ontology_id = report.get("induced_ontology_id")
    if not ontology_id:
        console.print("  [yellow]No induced ontology ID — skipping benchmark.[/]")
        console.print()
        return

    # ── Decide which golden to use ────────────────────────────
    golden_label = None
    golden_source = None  # URL or path
    for ds_id in datasource_ids:
        if ds_id in _KNOWN_GOLDEN_URLS:
            label, url = _KNOWN_GOLDEN_URLS[ds_id]
            golden_label = label
            golden_source = url
            console.print(f"  Auto-detected benchmark: [bold]{label}[/]")
            console.print(f"  Fetching golden from {url}")
            break

    if not golden_source:
        response = Prompt.ask(
            "  Score against a golden ontology? ([bold]path[/], [bold]url[/], or [bold]skip[/])",
            default="skip",
        ).strip()
        if response.lower() in ("", "skip", "n", "no"):
            console.print("  [dim]Skipping benchmark.[/]")
            console.print()
            return
        golden_source = response
        golden_label = response

    # ── Fetch golden ontology bytes ──────────────────────────
    try:
        if golden_source.startswith(("http://", "https://")):
            r = httpx.get(golden_source, timeout=30, follow_redirects=True)
            r.raise_for_status()
            golden_turtle = r.text
        else:
            golden_turtle = Path(golden_source).read_text()
    except Exception as e:
        console.print(f"  [red]Couldn't load golden ontology: {e}[/]")
        console.print()
        return

    # ── Fetch induced ontology Turtle ────────────────────────
    encoded_id = quote(ontology_id, safe="")
    try:
        induced_turtle = _download_ontology_turtle(encoded_id)
    except httpx.HTTPError as e:
        console.print(f"  [red]Couldn't download induced ontology {ontology_id}: {e}[/]")
        console.print()
        return

    # ── Import scorer (deferred so demo.py still runs if engine
    #    package isn't importable — e.g. someone copied just demo/*).
    try:
        from coa_ontology.benchmark import compare_to_golden
    except ImportError as e:
        console.print(f"  [yellow]Scorer unavailable: {e}[/]")
        console.print()
        return

    # ── Build a tier classifier from the catalog ─────────────
    # Enumerate every registered ontology once; for each, remember its URI
    # and its ontology_type. The classifier then does prefix matching
    # to decide if a grounded IRI belongs to the customer or foundational
    # tier. Without this, the scorecard's grounded_customer/foundational
    # counts stay at zero even when grounding edges exist.
    tier_prefixes: dict[str, str] = {}  # uri_prefix → 'customer' | 'foundational'
    try:
        for ont_type in ("customer_provided", "foundational"):
            onts = _list_ontologies(ontology_type=ont_type)
            tier = "customer" if ont_type == "customer_provided" else "foundational"
            for o in onts:
                uri = (o.get("uri") or "").rstrip("/#")
                if uri:
                    tier_prefixes[uri] = tier
    except Exception as e:  # noqa: BLE001 — classifier is best-effort diagnostic
        console.print(f"  [dim]Tier classifier unavailable ({e}) — scorecard will show total grounded only.[/]")
        tier_prefixes = {}

    def _classify_iri(iri: str) -> str:
        # Prefix match; longest-match wins so "https://schema.org/"
        # doesn't shadow "https://schema.org/version/latest/..." etc.
        best_prefix = ""
        best_tier = ""
        for prefix, tier in tier_prefixes.items():
            if iri.startswith(prefix) and len(prefix) > len(best_prefix):
                best_prefix = prefix
                best_tier = tier
        return best_tier

    try:
        score = compare_to_golden(
            induced_turtle,
            golden_turtle,
            tier_classifier=_classify_iri if tier_prefixes else None,
        )
    except Exception as e:
        console.print(f"  [red]Comparison failed: {e}[/]")
        console.print()
        return

    # ── Render scorecard ─────────────────────────────────────
    d = score.as_dict()

    summary_t = Table(show_header=True, title=f"Benchmark vs {golden_label}")
    summary_t.add_column("Layer")
    summary_t.add_column("Golden", justify="right")
    summary_t.add_column("Induced", justify="right")
    summary_t.add_column("Exact", justify="right")
    summary_t.add_column("Aligned", justify="right")
    summary_t.add_column("Missing", justify="right")
    summary_t.add_column("Novel", justify="right")
    summary_t.add_column("Precision", justify="right")
    summary_t.add_column("Recall", justify="right")
    summary_t.add_column("F1", justify="right")
    # Only show the tier-split column when a classifier was actually used —
    # otherwise the counts are all zero and the column is noise.
    show_tier_col = bool(tier_prefixes)
    if show_tier_col:
        summary_t.add_column("Grounded (cust / fnd)", justify="right")

    for layer in ("class", "property"):
        s = d[layer]
        row = [
            layer.capitalize(),
            str(s["total_golden"]),
            str(s["total_induced"]),
            f"[green]{s['exact']}[/]",
            f"[cyan]{s['aligned']}[/]",
            f"[yellow]{s['missing']}[/]",
            f"[red]{s['novel']}[/]",
            f"{s['precision']:.2f}",
            f"{s['recall']:.2f}",
            f"[bold]{s['f1']:.2f}[/]",
        ]
        if show_tier_col:
            if layer == "class":
                gc = s.get("grounded_customer", 0)
                gf = s.get("grounded_foundational", 0)
                row.append(f"{s['grounded']} ([magenta]{gc}[/] / [cyan]{gf}[/])")
            else:
                # property layer doesn't split by tier today
                row.append(f"{s['grounded']} [dim](not split)[/]")
        summary_t.add_row(*row)

    # Combined F1 row — fill trailing columns with empty strings
    combined_row = [
        "[bold]Combined F1[/]",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        f"[bold magenta]{d['combined_f1']:.2f}[/]",
    ]
    if show_tier_col:
        combined_row.append("")
    summary_t.add_row(*combined_row)
    console.print(summary_t)
    if show_tier_col:
        console.print(
            "  [dim]'Grounded (cust / fnd)' = classes the induction pipeline grounded to your "
            "customer-provided ontology vs to a cross-industry foundational ontology.[/]"
        )
    console.print()

    # ── Per-class detail (useful for debugging) ──────────────
    if Confirm.ask("  Show per-class breakdown?", default=False):
        ct = Table(show_header=True, title="Per-class alignment")
        ct.add_column("Golden class")
        ct.add_column("Status")
        ct.add_column("Induced match")
        ct.add_column("Grounded to")
        for m in score.class_matches:
            status_color = {
                "exact": "green",
                "aligned": "cyan",
                "missing": "red",
            }.get(m.match_type, "white")
            ct.add_row(
                m.golden_label or m.golden_uri.rsplit("/", 1)[-1],
                f"[{status_color}]{m.match_type}[/]",
                (m.induced_label or m.induced_uri.rsplit("/", 1)[-1]) if m.induced_uri else "—",
                m.grounded_to.rsplit("/", 1)[-1] if m.grounded_to else "—",
            )
        console.print(ct)
        console.print()

    # Stash for downstream use (e.g. write to file)
    report["_benchmark_score"] = d
    console.print()


# ── Step 4a: Accept proposal (auto or prompted) ────────────────


def _accept_proposal(proposal_id: str) -> str | None:
    """Accept the induction proposal and return the accepted ontology's URI.

    POSTs to /ontology/proposals/<id>/accept which ingests the proposal Turtle
    into the graph store and returns the real ontology_id (the uri_prefix the
    user supplied in Step 3). That URI is what /ontologies/{id}/download
    and /validate endpoints expect — without this step, Step 4b and Step 5
    404 because the induction pipeline only stores a proposal, not an
    accepted ontology.

    Note: the router is mounted at /ontology/proposals/ in main.py (see
    app.include_router(proposals_router, prefix="/ontology/proposals", ...).
    An earlier version of this function used /proposals/ and hit 404.

    Returns None if acceptance fails so the caller can skip downstream
    steps that depend on the accepted ontology existing.
    """
    try:
        resp = httpx.post(f"{CATALOG_URL}/ontology/proposals/{proposal_id}/accept", timeout=120)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        console.print(f"  [red]Couldn't accept proposal {proposal_id}: {e}[/]")
        return None
    status = data.get("status")
    ontology_id = data.get("ontology_id")
    if status not in ("accepted", "already_accepted"):
        console.print(f"  [yellow]Accept returned status={status}; skipping downstream steps.[/]")
        return None
    console.print(
        f"  [green]✓ Proposal accepted.[/] "
        f"Ontology URI: [bold]{ontology_id}[/]  "
        f"({data.get('classes_ingested', 0)} classes, "
        f"{data.get('properties_ingested', 0)} properties ingested)"
    )
    return ontology_id


def accept_induced_proposal(report: dict):
    """Prompt the user to accept the proposal, then swap the proposal_id
    in the report for the accepted ontology URI so downstream steps
    (benchmark, validation, export) can find it.
    """
    console.rule("[bold cyan]Step 4a · Accept Induced Ontology")

    proposal_id = report.get("induced_ontology_id")
    if not proposal_id:
        console.print("  [yellow]No proposal ID in report — skipping.[/]")
        console.print()
        return

    console.print(
        "  The induction pipeline created a [bold]proposal[/] pending review. "
        "Accepting turns it into a registered ontology: its classes + properties "
        "get embedded into OpenSearch (so future inductions can ground against them), "
        "the full Turtle is loaded into Neptune, and Step 4b / Step 5 / Export can "
        "read it."
    )
    console.print()

    if not Confirm.ask("  Accept this proposal?", default=True):
        console.print("  [dim]Skipped. The proposal stays in DynamoDB in 'pending' status.[/]")
        console.print(
            "  [dim]Downstream steps (benchmark, validation, export) will fail because "
            "they query /ontologies/ which only returns accepted ones.[/]"
        )
        console.print()
        return

    with timed("accept_proposal"):
        ontology_id = _accept_proposal(proposal_id)

    if ontology_id:
        # Swap the id so downstream code finds the accepted ontology
        report["induced_ontology_id"] = ontology_id
        report["_proposal_id"] = proposal_id  # preserve for reference
        report["_accepted"] = True
    else:
        report["_accepted"] = False

    console.print()


# ── Step 5: Validation ─────────────────────────────────────────


def run_validation(report: dict):
    ontology_id = report.get("induced_ontology_id")
    if not ontology_id:
        console.print("  [yellow]No induced ontology ID — skipping validation.[/]")
        return

    if not Confirm.ask("Run validation on the induced ontology?", default=True):
        return

    console.rule("[bold cyan]Step 5 · Validation")

    payload = {
        "ontology_id": ontology_id,
        "tiers": ["tier1_blocking", "tier2_scoring", "tier3_review"],
    }

    with timed("validation_submit"):
        job = _http_fetch("POST", f"{VALIDATION_URL}/validate/", json=payload, timeout=30)

    job_id = job["job_id"]
    validation_start = time.perf_counter()

    with Live(Spinner("dots", text="  Validating…"), console=console, refresh_per_second=4):
        while True:
            r = httpx.get(f"{VALIDATION_URL}/validate/jobs/{job_id}", timeout=10).json()
            if r["status"] in ("completed", "failed"):
                break
            time.sleep(0.5)

    timings["validation_total"] = time.perf_counter() - validation_start

    if r["status"] == "failed":
        console.print(f"  [red]Validation failed: {r.get('error')}[/]")
        return

    vr = r.get("report", {})
    passed = vr.get("passed", False)
    status_text = "[bold green]PASSED[/]" if passed else "[bold red]FAILED[/]"
    console.print(f"  Result: {status_text}")
    console.print()

    # Findings table
    findings = vr.get("findings", [])
    if findings:
        ft = Table(show_header=True, title="Findings")
        ft.add_column("Tier")
        ft.add_column("Severity")
        ft.add_column("Validator")
        ft.add_column("Message")

        sev_colors = {"error": "red", "warning": "yellow", "info": "blue"}
        for f in findings:
            color = sev_colors.get(f["severity"], "white")
            ft.add_row(f["tier"], f"[{color}]{f['severity']}[/]", f["validator"], f["message"])

        console.print(ft)
        console.print()

    # Metrics
    metrics = vr.get("metrics")
    if metrics:
        mt = Table(show_header=True, title="Structural Metrics (OntoQA)")
        mt.add_column("Metric")
        mt.add_column("Value", justify="right")
        for k, v in metrics.items():
            if v is not None:
                display = f"{v:.3f}" if isinstance(v, float) else str(v)
                mt.add_row(k.replace("_", " ").title(), display)
        console.print(mt)
        console.print()


# ── Summary ─────────────────────────────────────────────────────


def show_summary():
    console.rule("[bold cyan]Timing Summary")

    t = Table(show_header=True)
    t.add_column("Step")
    t.add_column("Duration", justify="right")

    for label, elapsed in timings.items():
        t.add_row(label, f"{elapsed:.3f}s")

    total = sum(timings.values())
    t.add_row("[bold]Total wall time[/]", f"[bold]{total:.3f}s[/]")

    console.print(t)
    console.print()


# ── Main ────────────────────────────────────────────────────────


def export_ontology():
    """Let the user select an induced ontology and display its Turtle content."""
    console.rule("[bold cyan]Export Ontology")

    ontologies = _list_ontologies(ontology_type="induced")

    if not ontologies:
        console.print("  [yellow]No induced ontologies found.[/]")
        return

    t = Table(show_header=True, title="Induced Ontologies")
    t.add_column("#", style="bold")
    t.add_column("ID", justify="right")
    t.add_column("Title")
    t.add_column("URI")

    for i, o in enumerate(ontologies, 1):
        t.add_row(str(i), str(o["ontologyId"]), o["title"], o["uri"])

    console.print(t)
    console.print()

    selection = Prompt.ask(
        "Which ontology to export? (number or [bold]none[/])",
        default="1",
    )
    if selection.strip().lower() == "none":
        return

    try:
        idx = int(selection.strip()) - 1
    except ValueError:
        console.print(f"  [yellow]Couldn't parse {selection!r} as a number.[/]")
        return
    if idx < 0 or idx >= len(ontologies):
        console.print("  [red]Invalid selection.[/]")
        return

    onto = ontologies[idx]
    encoded_id = quote(onto["ontologyId"], safe="")
    try:
        turtle = _download_ontology_turtle(encoded_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"  [yellow]No file uploaded for ontology {onto['id']}.[/]")
        else:
            console.print(f"  [red]Error downloading ontology: {e}[/]")
        return
    except httpx.HTTPError as e:
        console.print(f"  [red]Network error downloading ontology: {e}[/]")
        return

    console.print()
    console.print(Panel(turtle, title=f"[bold]{onto['title']}[/] — Turtle", border_style="green"))
    console.print()


def main():
    console.print(
        Panel.fit(
            "[bold]Ontology Induction Platform[/] — Interactive Demo",
            subtitle="Ctrl+C to exit at any time",
        )
    )
    console.print()

    check_health()
    datasource_ids = browse_data_catalog()
    grounding_ids = browse_ontology_catalog()
    job = run_induction(datasource_ids, grounding_ids)
    report = show_results(job)
    if report:
        # Accept the proposal before Step 4b / validation / export. Without
        # this, the induced_ontology_id in the report is still a proposal_id
        # that /ontologies/ can't resolve — 404s on download / validate.
        accept_induced_proposal(report)
        benchmark_against_golden(report, datasource_ids)
        run_validation(report)
    show_summary()
    export_ontology()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
