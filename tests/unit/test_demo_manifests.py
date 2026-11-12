# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Static checks of every ``demo/<industry>/manifest.yaml`` against the files it names.

onboard-demo.sh reads these manifests only at onboard time, against a deployed
stack, and some mistakes there only log an ``[!]`` line (a steward edit on a
column that does not exist, a metric over a table that is not loaded). So the
references are pinned here: files exist, source names resolve, steward edits
and metrics name real Glue tables and columns, and the metrics / questions JSON
has the shape the script's jq expects. No AWS calls.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_DEMO = Path(__file__).resolve().parents[2] / "demo"
_MANIFESTS = sorted(_DEMO.glob("*/manifest.yaml"))

if not _MANIFESTS:
    pytest.skip("no demo manifests in this checkout", allow_module_level=True)


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path} is not a mapping"
    return data


def _glue_columns(manifest: Path, mf: dict[str, Any]) -> dict[str, set[str]]:
    """Glue table name -> column names, from each table's ``table.json``."""
    out: dict[str, set[str]] = {}
    for t in (mf.get("data") or {}).get("glue", {}).get("tables", []) or []:
        tdef = json.loads((manifest.parent / t["table"]).read_text(encoding="utf-8"))
        sd = tdef.get("StorageDescriptor", {})
        cols = {c["Name"] for c in sd.get("Columns", [])} | {c["Name"] for c in tdef.get("PartitionKeys", [])}
        out[t["name"]] = cols
    return out


def _metrics(manifest: Path, mf: dict[str, Any]) -> list[dict[str, Any]]:
    if not mf.get("metrics"):
        return []
    data = json.loads((manifest.parent / mf["metrics"]).read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data["metrics"]


_ids = [p.parent.name for p in _MANIFESTS]


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=_ids)
def test_every_referenced_file_exists(manifest: Path) -> None:
    mf = _load(manifest)
    data = mf.get("data") or {}
    refs: list[str] = []
    for t in (data.get("glue") or {}).get("tables", []) or []:
        refs += [t[k] for k in ("file", "csv", "table") if k in t]
    refs += [o["file"] for o in (mf.get("ontologies") or {}).get("upload", []) or []]
    refs += [mf[k] for k in ("metrics", "questions") if mf.get(k)]
    missing = [r for r in refs if not (manifest.parent / r).is_file()]
    assert not missing, f"{manifest.parent.name}: missing {missing}"


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=_ids)
def test_documents_dir_has_files_matching_the_include_globs(manifest: Path) -> None:
    docs = (_load(manifest).get("data") or {}).get("documents")
    if not docs:
        pytest.skip("no documents")
    d = manifest.parent / docs["dir"]
    assert d.is_dir(), d
    globs = docs.get("include") or ["*.md"]
    assert any(any(d.rglob(g)) for g in globs), f"no file in {d} matches {globs}"


# The `from` kinds onboard-demo.sh's build_source_body accepts, and the source
# type each one registers as. Any other `from` fails the run at registration.
_FROM_TYPES = {
    "postgres": "DATABASE",
    "jdbc": "DATABASE",
    "glue": "DATABASE",
    "glue-static": "DATABASE",
    "documents": "DOCUMENTS",
}


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=_ids)
def test_every_source_has_a_known_from_kind_matching_its_type(manifest: Path) -> None:
    """A `from:` typo would otherwise only turn the glue-static checks below into skips."""
    for s in _load(manifest).get("sources", []) or []:
        assert s.get("from") in _FROM_TYPES, f"{s['name']}: unknown from {s.get('from')!r}"
        assert s.get("type") == _FROM_TYPES[s["from"]], f"{s['name']}: from {s['from']} is a {_FROM_TYPES[s['from']]}"


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=_ids)
def test_source_names_resolve(manifest: Path) -> None:
    mf = _load(manifest)
    names = {s["name"] for s in mf.get("sources", []) or []}
    if mf.get("metricsSource"):
        assert mf["metricsSource"] in names
    for e in (mf.get("steward") or {}).get("edits", []) or []:
        # The script applies a source-less edit to every source; a shipped pack
        # names its source so the edit cannot land on the wrong one.
        assert e.get("source") in names, f"steward edit needs a source naming one of {sorted(names)}: {e}"


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=_ids)
def test_steward_edits_name_real_glue_columns(manifest: Path) -> None:
    mf = _load(manifest)
    cols = _glue_columns(manifest, mf)
    edits = (mf.get("steward") or {}).get("edits", []) or []
    if not edits:
        pytest.skip("no steward edits")
    for e in edits:
        table = e["table"].split(".")[-1]
        assert table in cols, f"steward edit table {e['table']!r} is not a Glue table"
        assert e["column"] in cols[table], f"{table} has no column {e['column']!r}"
        assert e.get("description"), e


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=_ids)
def test_metrics_and_questions_have_the_shape_the_script_reads(manifest: Path) -> None:
    mf = _load(manifest)
    metrics = _metrics(manifest, mf)
    for m in metrics:
        assert m.get("name"), m
        if isinstance(m.get("expression"), dict):
            dialects = m["expression"].get("dialects") or []
            assert dialects and dialects[0]["dialect"] == "TRINO", m["name"]
            assert all(d.get("expression") for d in dialects), m["name"]
        else:
            assert m.get("trinoExpression"), m["name"]
    if mf.get("questions"):
        q = json.loads((manifest.parent / mf["questions"]).read_text(encoding="utf-8"))
        assert isinstance(q, (list, dict)) and q, "questions file is empty"


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=_ids)
def test_glue_static_metrics_reference_loaded_tables(manifest: Path) -> None:
    """A metric over a table the pack does not load would fail only at query time."""
    mf = _load(manifest)
    src = next((s for s in mf.get("sources", []) or [] if s["name"] == mf.get("metricsSource")), None)
    if not src or src.get("from") != "glue-static":
        pytest.skip("metrics are not on a glue-static source")
    cols = _glue_columns(manifest, mf)
    for m in _metrics(manifest, mf):
        if m.get("sourceTable"):
            assert m["sourceTable"] in cols, f"{m['name']}: sourceTable {m['sourceTable']!r} is not loaded"
        sql = " ".join(d["expression"] for d in (m.get("expression") or {}).get("dialects", []))
        for table in re.findall(r"\{db\}\.(\w+)", sql):
            assert table in cols, f"{m['name']}: {{db}}.{table} is not a Glue table of this pack"


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=_ids)
def test_uploaded_grounding_iris_are_uploaded(manifest: Path) -> None:
    """An IRI in groundingOntologyIds that matches no upload or catalog key grounds against nothing."""
    mf = _load(manifest)
    uploaded = {o["iri"] for o in (mf.get("ontologies") or {}).get("upload", []) or []}
    ids = (mf.get("induction") or {}).get("groundingOntologyIds", []) or []
    for i in ids:
        if i.startswith("https://anycompany.example/"):
            assert i in uploaded, f"{i} is listed for grounding but not in ontologies.upload"
