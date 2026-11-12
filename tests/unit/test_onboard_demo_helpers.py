# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for onboard-demo.sh helpers that decide what a run reports.

Each helper is extracted from the script (never copied, so a test cannot drift
from the shipped code) and run in bash with the API calls stubbed. No AWS calls.

- token refresh (``jwt_claim`` / ``set_token`` / ``current_token``): the token
  lives in a file because most API calls run in ``$(...)`` subshells, so a
  refresh must survive across them;
- ``steward_edits`` / ``source_database``: which table a steward edit PATCHes;
- the metric-body jq program: ``{db}`` / ``{namespaceId}`` substitution and the
  legacy ``trinoExpression`` shape;
- ``put_metric``: a rejected update PUT is reported, not printed as "updated";
- ``induct_source``: a failed proposal lookup or accept fails the induction;
- ``approve_and_induct``: ``steward.autoApprove: false`` leaves the source in review;
- ``upload_ontologies`` / ``failed_grounding``: a failed ontology upload or
  ingest is recorded, and a source that grounds on it is not inducted;
- ``ensure_docs_bucket``: the hardening is re-asserted on an existing bucket,
  every call but CreateBucket names the expected owner;
- ``ensure_grant``: an existing grant is kept, with a warning when its scope
  differs from the manifest's;
- ``fail_step`` / ``finish``: every ``[!]`` path is recorded and the run exits
  non-zero when any was.

The jq-backed tests skip when jq is not installed, except under CI (``CI`` set),
where they fail instead: the unit-test jobs install jq, so a skip there would
mean these tests silently stopped running.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "onboard-demo.sh"

if not _SCRIPT.exists():
    pytest.skip("scripts/onboard-demo.sh is absent from this checkout", allow_module_level=True)

_TEXT = _SCRIPT.read_text(encoding="utf-8")
_NS = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def _jq() -> None:
    """jq, for the tests that run the script's jq: required under CI, else a skip."""
    if shutil.which("jq") is not None:
        return
    if os.environ.get("CI"):
        pytest.fail("jq is not installed; the CI unit-test jobs install it before tests/unit (ci/merge-request.yml)")
    pytest.skip("jq is not installed")


needs_jq = pytest.mark.usefixtures("_jq")


def _fn(name: str, indent: str = "") -> str:
    """The definition of shell function *name* (top-level, or nested at *indent*)."""
    head = rf"^{indent}{re.escape(name)}\(\) \{{"
    # One-line definitions first (`name() { ...; }  # comment`), then multi-line ones.
    match = re.search(rf"{head}.*; \}}\s*(#.*)?$", _TEXT, re.MULTILINE)
    if match is None:
        match = re.search(rf"{head}.*?^{indent}\}}", _TEXT, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in onboard-demo.sh — did it get renamed?"
    return match.group(0)


def _failure_fns(failures: Path) -> str:
    """``record_failure`` / ``fail_step``, writing to *failures*."""
    return f'FAILURES="{failures}"; : > "$FAILURES"\n' + _fn("record_failure") + "\n" + _fn("fail_step")


def _bash(script: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script, "bash", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


# ─── Token refresh ────────────────────────────────────────────────────────────


def _jwt(exp: int, sub: str) -> str:
    def seg(obj: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg({'exp': exp, 'sub': sub})}.sig"


def _token_run(
    tmp_path: Path, token: str, can_refresh: bool, prints: str | None = None, timeout: int = 60
) -> tuple[list[str], int, str]:
    """Two separate ``$(current_token)`` calls; returns (outputs, mint count, stderr).

    The --token-cmd prints a fresh JWT, or *prints* (a shell snippet) instead.
    """
    fresh = _jwt(int(time.time()) + 3600, "minted")
    counter = tmp_path / "mints"
    counter.write_text("")
    mint_cmd = f"echo x >> {counter}; " + (prints if prints is not None else f"echo {fresh}")
    fns = "\n".join(
        _fn(n) for n in ("jwt_claim", "set_token", "with_timeout", "mint_token", "refresh_token", "current_token")
    )
    script = f"""set -euo pipefail
WORK_DIR="{tmp_path}"; TOKEN_FILE="$WORK_DIR/token"
{_failure_fns(tmp_path / "failures")}
TOKEN_CMD='{mint_cmd}'; CAN_REFRESH="{"true" if can_refresh else "false"}"; TOKEN_CMD_TIMEOUT={timeout}
sleep() {{ echo "sleep $1" >> "$WORK_DIR/sleeps"; }}
{fns}
set_token "$1" || true
[[ -e "$TOKEN_FILE" ]] || printf '%s' "$1" > "$TOKEN_FILE"
a=$(current_token); b=$(current_token)
printf '%s\\n%s\\n' "$a" "$b"
"""
    proc = _bash(script, token)
    assert proc.returncode == 0, proc.stderr
    mints = len(counter.read_text().splitlines())
    return proc.stdout.split(), mints, proc.stderr


def test_token_near_expiry_is_refreshed_once_and_persists_across_subshells(tmp_path: Path) -> None:
    old = _jwt(int(time.time()) + 60, "old")
    outputs, mints, _ = _token_run(tmp_path, old, can_refresh=True)
    assert mints == 1, "the refresh must be written to the token file, not a subshell variable"
    assert outputs[0] == outputs[1] != old
    assert json.loads(base64.urlsafe_b64decode(outputs[0].split(".")[1] + "=="))["sub"] == "minted"


def test_token_with_plenty_of_life_is_not_refreshed(tmp_path: Path) -> None:
    tok = _jwt(int(time.time()) + 3600, "old")
    outputs, mints, _ = _token_run(tmp_path, tok, can_refresh=True)
    assert mints == 0
    assert outputs == [tok, tok]


def test_expired_fixed_token_warns_once_and_is_still_used(tmp_path: Path) -> None:
    tok = _jwt(int(time.time()) - 600, "old")
    outputs, mints, stderr = _token_run(tmp_path, tok, can_refresh=False)
    assert mints == 0
    assert outputs == [tok, tok]
    assert stderr.count("expired") == 1, stderr
    assert "expired" in (tmp_path / "failures").read_text(), "an expired token fails the run"


@pytest.mark.parametrize("bad", ["", "   ", "None", "null"])
def test_set_token_rejects_an_empty_or_null_token(tmp_path: Path, bad: str) -> None:
    script = f"""set -euo pipefail
TOKEN_FILE="{tmp_path}/token"
{_fn("jwt_claim")}
{_fn("set_token")}
rc=0; set_token "$1" || rc=$?
echo "rc=$rc"; [[ -e "$TOKEN_FILE" ]] && echo written || true
"""
    proc = _bash(script, bad)
    assert proc.stdout.split() == ["rc=1"], proc.stdout


@pytest.mark.parametrize("prints", ["exit 3", "echo null", "echo"], ids=["fails", "null", "empty"])
def test_a_failing_refresh_is_bounded_and_backs_off(tmp_path: Path, prints: str) -> None:
    old = _jwt(int(time.time()) - 60, "old")
    outputs, mints, stderr = _token_run(tmp_path, old, can_refresh=True, prints=prints)
    assert mints == 3, "3 attempts in the first call; the second call is inside the one-minute hold"
    assert (tmp_path / "sleeps").read_text().split() == ["sleep", "5", "sleep", "10"]
    assert outputs == [old, old]
    assert (tmp_path / "failures").read_text().count("token refresh failed") == 1, "recorded once"


def test_a_failed_refresh_with_time_left_only_warns(tmp_path: Path) -> None:
    old = _jwt(int(time.time()) + 120, "old")
    outputs, mints, stderr = _token_run(tmp_path, old, can_refresh=True, prints="exit 3")
    assert mints == 3 and outputs == [old, old]
    assert "WARN: token refresh failed" in stderr
    assert (tmp_path / "failures").read_text() == "", "the current token still works"


def test_a_hung_token_cmd_is_killed_by_the_timeout(tmp_path: Path) -> None:
    old = _jwt(int(time.time()) - 60, "old")
    start = time.monotonic()
    _, mints, _ = _token_run(tmp_path, old, can_refresh=True, prints="exec sleep 30", timeout=1)
    assert mints == 3
    assert time.monotonic() - start < 20, "each attempt is cut off after TOKEN_CMD_TIMEOUT"


@pytest.mark.parametrize("has_timeout", [True, False], ids=["timeout(1)", "python-fallback"])
def test_with_timeout_passes_output_and_status_through_and_kills_a_hang(tmp_path: Path, has_timeout: bool) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tools = ["bash", "python3", "sleep"] + (["timeout"] if has_timeout else [])
    for tool in tools:
        found = shutil.which(tool)
        assert found, tool
        (bindir / tool).symlink_to(found)
    script = f"""PATH="{bindir}"
{_fn("with_timeout")}
out=$(with_timeout 5 bash -c 'echo hi; exit 3'); echo "out=$out rc=$?"
with_timeout 1 sleep 30; echo "hung rc=$?"
"""
    start = time.monotonic()
    proc = _bash(script)
    assert proc.stdout.split("\n")[:2] == ["out=hi rc=3", "hung rc=124"], proc.stdout + proc.stderr
    assert time.monotonic() - start < 10


def test_non_jwt_token_never_refreshes(tmp_path: Path) -> None:
    outputs, mints, _ = _token_run(tmp_path, "not-a-jwt", can_refresh=True)
    assert mints == 0
    assert outputs == ["not-a-jwt", "not-a-jwt"]


# ─── Steward edits ────────────────────────────────────────────────────────────

_MF: dict[str, object] = {
    "data": {"glue": {"databaseName": "anycompany_bank"}},
    "sources": [
        {"name": "wh", "type": "DATABASE", "from": "glue-static"},
        {"name": "other", "type": "DATABASE", "from": "glue-static"},
    ],
    "steward": {
        "edits": [
            {"source": "wh", "table": "account", "column": "status", "description": "bare"},
            {"source": "wh", "table": "x_db.loan", "column": "rate", "description": "qualified"},
            {"table": "customer", "column": "email", "description": "no source"},
            {"source": "other", "table": "card", "column": "pan", "description": "other source"},
        ]
    },
}


def _steward_run(
    tmp_path: Path, mf: dict[str, object], name: str, glue_db: str = "", patch_codes: str = "200"
) -> tuple[int, list[tuple[str, dict[str, object]]], str]:
    """Run steward_edits; returns (rc, [(PATCH path, body)], recorded failures).

    *patch_codes* is the HTTP code of each PATCH in turn (the last one repeats).
    """
    override = re.search(r'^\[\[ -n "\$GLUE_DB_OVERRIDE" \]\].*$', _TEXT, re.MULTILINE)
    assert override, "--glue-db override line not found"
    fns = "\n".join(_fn(n) for n in ("mf", "source_database", "steward_edits"))
    calls = tmp_path / "patches"
    script = f"""set -euo pipefail
MF="$1"; GLUE_DB_OVERRIDE="$2"; NS="{_NS}"; S3_GLUE_DB=""; SCHEMA_FILTER=""; RESP=/dev/null
{_failure_fns(tmp_path / "failures")}
{override.group(0)}
{fns}
codes=({patch_codes}); : > "{calls}"
api_patch() {{  # runs in $(...): the call count is the log's line count
  local n; n=$(wc -l < "{calls}")
  jq -nc --arg p "$1" --argjson b "$2" '[$p, $b]' >> "{calls}"
  echo "${{codes[$(( n < ${{#codes[@]}} ? n : ${{#codes[@]}} - 1 ))]}}"
}}
rc=0; steward_edits sid1 "$3" >/dev/null 2>&1 || rc=$?
echo "rc=$rc"
"""
    proc = _bash(script, json.dumps(mf), glue_db, name)
    assert proc.returncode == 0, proc.stderr
    rc = int(re.search(r"rc=(\d+)", proc.stdout).group(1))  # type: ignore[union-attr]
    patches = [tuple(json.loads(line)) for line in calls.read_text().splitlines()]
    return rc, patches, (tmp_path / "failures").read_text()  # type: ignore[return-value]


def _steward_paths(mf: dict[str, object], name: str, glue_db: str = "") -> list[str]:
    with tempfile.TemporaryDirectory() as d:
        rc, patches, _ = _steward_run(Path(d), mf, name, glue_db)
    assert rc == 0
    return [p for p, _ in patches]


def _tables(paths: list[str]) -> list[str]:
    return [p.split("/tables/")[1].split("/columns/")[0] for p in paths]


@needs_jq
def test_steward_edit_qualifies_a_bare_table_with_the_glue_database() -> None:
    tables = _tables(_steward_paths(_MF, "wh"))
    assert tables == ["anycompany_bank.account", "x_db.loan", "anycompany_bank.customer"]


@needs_jq
def test_steward_edit_uses_the_glue_db_override() -> None:
    tables = _tables(_steward_paths(_MF, "wh", glue_db="bank_scltest"))
    assert tables[0] == "bank_scltest.account"
    assert tables[1] == "x_db.loan", "an already-qualified table is left unchanged"


@needs_jq
def test_steward_edit_without_source_applies_to_every_source_and_others_are_skipped() -> None:
    tables = _tables(_steward_paths(_MF, "other"))
    assert tables == ["anycompany_bank.customer", "anycompany_bank.card"]


@needs_jq
def test_steward_edit_patches_the_column_metadata_with_the_description(tmp_path: Path) -> None:
    mf: dict[str, object] = {
        "data": {"glue": {"databaseName": "bank"}},
        "sources": [{"name": "wh", "type": "DATABASE", "from": "glue-static"}],
        "steward": {"edits": [{"source": "wh", "table": "loan", "column": "apr rate", "description": 'APR, "annual"'}]},
    }
    rc, patches, recorded = _steward_run(tmp_path, mf, "wh")
    assert rc == 0 and recorded == ""
    assert patches == [
        (
            f"/namespaces/{_NS}/sources/sid1/tables/bank.loan/columns/apr%20rate/metadata",
            {"overrides": {"description": 'APR, "annual"'}},
        ),
    ]


@needs_jq
def test_a_failed_steward_patch_is_recorded_and_the_rest_still_run(tmp_path: Path) -> None:
    rc, patches, recorded = _steward_run(tmp_path, _MF, "wh", patch_codes="500 200")
    assert rc == 1, "a failed PATCH must fail steward_edits"
    assert len(patches) == 3, "the remaining edits are still attempted"
    assert "steward edit anycompany_bank.account.status -> HTTP 500" in recorded
    assert recorded.count("\n") == 1


@needs_jq
def test_a_failed_steward_edit_stops_before_the_approve() -> None:
    script = f"""set -euo pipefail
NS="{_NS}"; FORCE_INDUCE=false; MF='{{"steward":{{}}}}'
{_fn("mf")}
source_already_inducted() {{ return 1; }}
source_status() {{ echo PENDING_REVIEW; }}
steward_edits() {{ return 1; }}
poll_status() {{ echo APPROVED; }}
api_post() {{ echo "POST $1" >&2; }}
induct_source() {{ echo INDUCT; }}
{_fn("approve_and_induct")}
rc=0; approve_and_induct sid1 sfx wh 2>&1 || rc=$?
echo "rc=$rc"
"""
    proc = _bash(script)
    assert proc.returncode == 0, proc.stderr
    assert "rc=1" in proc.stdout
    assert "/approve" not in proc.stdout and "INDUCT" not in proc.stdout
    assert "left in PENDING_REVIEW so the next run retries" in proc.stdout


# ─── Metric bodies ────────────────────────────────────────────────────────────


def _metric_program() -> str:
    match = re.search(
        r"""< <\(jq -c --arg ds "\$METRICS_SID" --arg ns "\$NS" --arg db "\$METRICS_DB" '(.*?)' "\$METRICS_PATH"\)""",
        _TEXT,
        re.DOTALL,
    )
    assert match, "metric-body jq program not found in onboard-demo.sh"
    return match.group(1)


def _metric_bodies(path: Path, db: str = "the_db") -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["jq", "-c", "--arg", "ds", "DS1", "--arg", "ns", _NS, "--arg", "db", db, _metric_program(), str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line]


_METRIC_FILES = sorted((_REPO_ROOT / "demo").glob("*/metrics/metrics.json"))


@needs_jq
@pytest.mark.parametrize("path", _METRIC_FILES, ids=lambda p: p.parent.parent.name)
def test_every_demo_metric_body_is_fully_substituted(path: Path) -> None:
    bodies = _metric_bodies(path)
    assert bodies, f"{path} produced no metric bodies"
    for body in bodies:
        text = json.dumps(body)
        assert "{db}" not in text and "{namespaceId}" not in text, body["name"]
        assert body["dataSourceId"] == "DS1"
        dialects = body["expression"]["dialects"]
        assert dialects and all(d.get("expression") for d in dialects), body["name"]
        assert dialects[0]["dialect"] == "TRINO", "the resolver uses dialects[0] and the firewall reads Trino"


@needs_jq
def test_object_expression_gets_db_and_namespace_substituted(tmp_path: Path) -> None:
    f = tmp_path / "m.json"
    f.write_text(
        json.dumps(
            {
                "metrics": [
                    {
                        "name": "m",
                        "expression": {"dialects": [{"dialect": "TRINO", "expression": "SELECT 1 FROM {db}.t"}]},
                        "ontologyConcepts": ["http://x/namespace/{namespaceId}/ontology#T"],
                    }
                ]
            }
        )
    )
    (body,) = _metric_bodies(f, db="glue_db")
    assert body["expression"]["dialects"][0]["expression"] == "SELECT 1 FROM glue_db.t"
    assert body["ontologyConcepts"] == [f"http://x/namespace/{_NS}/ontology#T"]
    assert body["dataSourceId"] == "DS1"


@needs_jq
def test_legacy_trino_expression_is_wrapped_in_a_trino_dialect(tmp_path: Path) -> None:
    f = tmp_path / "m.json"
    f.write_text(
        json.dumps(
            [
                {
                    "name": "m",
                    "description": "d",
                    "trinoExpression": "SELECT 2",
                    "sourceTable": "t",
                    "unit": "count",
                    "synonyms": ["s"],
                }
            ]
        )
    )
    (body,) = _metric_bodies(f)
    assert body["expression"] == {"dialects": [{"dialect": "TRINO", "expression": "SELECT 2"}]}
    assert body["aiContext"]["synonyms"] == ["s"]
    assert body["dataSourceId"] == "DS1"


@needs_jq
@pytest.mark.parametrize(("put_code", "expect"), [("200", "[~] m (updated)"), ("400", "[!] m PUT -> HTTP 400")])
def test_metric_update_reports_the_put_status(tmp_path: Path, put_code: str, expect: str) -> None:
    resp = tmp_path / "resp"
    resp.write_text("validation error")
    script = f"""set -euo pipefail
NS="{_NS}"; RESP="{resp}"
{_failure_fns(tmp_path / "failures")}
api_post() {{ echo 409; }}
api_put() {{ echo "{put_code}"; }}
{_fn("put_metric", indent="  ")}
put_metric '{{"name":"m"}}' 2>&1
"""
    proc = _bash(script)
    assert proc.returncode == 0, proc.stderr
    assert expect in proc.stdout
    recorded = (tmp_path / "failures").read_text()
    if put_code != "200":
        assert "updated" not in proc.stdout
        assert "m PUT -> HTTP 400" in recorded, "a rejected update must fail the run"
    else:
        assert recorded == ""


# ─── Proposal lookup + accept ─────────────────────────────────────────────────


def _induct(
    tmp_path: Path, prop_code: str, prop_body: str, listing: str, accept_code: str = "202"
) -> tuple[int, str, list[str]]:
    log = tmp_path / "calls"
    log.write_text("")
    resp = tmp_path / "resp"
    fns = "\n".join(_fn(n) for n in ("mf", "induction_cfg", "list_all_proposals", "proposal_id_of", "induct_source"))
    script = f"""set -euo pipefail
NS="{_NS}"; RESP="{resp}"; MF='{{"sources":[]}}'; LOG="{log}"
sleep() {{ :; }}
reject_inflight() {{ :; }}
api_post() {{
  echo "POST $1" >> "$LOG"
  case "$1" in
    */induce) echo '{{"job_id":"J1"}}' > "$RESP"; echo 202;;
    */accept) echo '{{}}' > "$RESP"; echo "{accept_code}";;
  esac
}}
api_curl() {{ echo "$1 $2" >> "$LOG"; printf '%s' '{prop_body}' > "$RESP"; echo "{prop_code}"; }}
api_get() {{
  case "$1" in
    */induce/jobs/*) echo '{{"status":"completed"}}';;
    */proposals)     printf '%s' '{listing}';;
    */proposals/*)   echo '{{"status":"accepted"}}';;
  esac
}}
{fns}
rc=0; induct_source sid1 sfx wh || rc=$?
echo "rc=$rc"
"""
    proc = _bash(script)
    assert proc.returncode == 0, proc.stderr
    rc = int(re.search(r"rc=(\d+)", proc.stdout).group(1))  # type: ignore[union-attr]
    return rc, proc.stdout, log.read_text().splitlines()


@needs_jq
def test_pending_job_proposal_is_accepted(tmp_path: Path) -> None:
    rc, out, calls = _induct(tmp_path, "200", '{"status":"pending"}', "[]")
    assert rc == 0
    assert "accepted proposal J1" in out
    assert f"POST /namespaces/{_NS}/proposals/J1/accept" in calls


@needs_jq
def test_failed_proposal_lookup_fails_the_induction(tmp_path: Path) -> None:
    rc, out, calls = _induct(tmp_path, "401", '{"message":"Unauthorized"}', "[]")
    assert rc == 1
    assert "lookup HTTP 401" in out
    assert not any(c.endswith("/accept") for c in calls)


@needs_jq
@pytest.mark.parametrize(
    "listing", ["", "<html>", '{"message":"Forbidden"}', "null"], ids=["empty", "html", "error", "null"]
)
def test_failed_proposal_listing_fails_the_induction(tmp_path: Path, listing: str) -> None:
    rc, out, calls = _induct(tmp_path, "404", '{"message":"not found"}', listing)
    assert rc == 1
    assert "could not list proposals" in out
    assert not any(c.endswith("/accept") for c in calls)


@needs_jq
def test_no_pending_proposal_after_a_good_lookup_is_an_idempotent_skip(tmp_path: Path) -> None:
    rc, out, _ = _induct(tmp_path, "404", '{"message":"not found"}', "[]")
    assert rc == 0
    assert "skipping accept" in out


@needs_jq
@pytest.mark.parametrize("status", ["rejected", "cancelled", "failed", "accept_failed"])
def test_a_closed_or_failed_job_proposal_fails_the_induction(tmp_path: Path, status: str) -> None:
    listing = '[{"proposal_id":"P2","status":"pending"}]'
    rc, out, calls = _induct(tmp_path, "200", f'{{"status":"{status}"}}', listing)
    assert rc == 1
    assert f"proposal J1 is {status}" in out
    assert not any(c.endswith("/accept") for c in calls), "an unrelated pending proposal is not accepted instead"


@needs_jq
def test_a_paged_listing_object_is_read(tmp_path: Path) -> None:
    rc, _, calls = _induct(tmp_path, "404", "{}", '{"proposals":[{"proposalId":"P3","status":"pending"}]}')
    assert rc == 0
    assert f"POST /namespaces/{_NS}/proposals/P3/accept" in calls


@needs_jq
def test_fallback_accepts_the_newest_pending_proposal(tmp_path: Path) -> None:
    listing = '[{"proposal_id":"P2","status":"pending"},{"proposal_id":"P1","status":"accepted"}]'
    rc, _, calls = _induct(tmp_path, "404", "{}", listing)
    assert rc == 0
    assert f"POST /namespaces/{_NS}/proposals/P2/accept" in calls


@needs_jq
def test_rejected_accept_post_fails_fast(tmp_path: Path) -> None:
    rc, out, _ = _induct(tmp_path, "200", '{"status":"pending"}', "[]", accept_code="400")
    assert rc == 1
    assert "accept J1 HTTP 400" in out


@needs_jq
def test_already_accepted_job_proposal_is_not_re_accepted(tmp_path: Path) -> None:
    rc, out, calls = _induct(tmp_path, "200", '{"status":"accepted"}', "[]")
    assert rc == 0
    assert "already accepted" in out
    assert not any(c.endswith("/accept") for c in calls)


# ─── steward.autoApprove ──────────────────────────────────────────────────────


@needs_jq
@pytest.mark.parametrize(
    ("steward", "approves"), [({"autoApprove": False}, False), ({"autoApprove": True}, True), ({}, True)]
)
def test_auto_approve_false_leaves_the_source_in_review(steward: dict[str, object], approves: bool) -> None:
    mf = json.dumps({"steward": steward})
    script = f"""set -euo pipefail
NS="{_NS}"; FORCE_INDUCE=false; MF='{mf}'
{_fn("mf")}
source_already_inducted() {{ return 1; }}
source_status() {{ echo PENDING_REVIEW; }}
steward_edits() {{ :; }}
poll_status() {{ echo APPROVED; }}
api_post() {{ echo "POST $1" >&2; }}  # the script discards api_post's stdout
induct_source() {{ echo INDUCT; }}
{_fn("approve_and_induct")}
approve_and_induct sid1 sfx wh 2>&1
"""
    proc = _bash(script)
    assert proc.returncode == 0, proc.stderr
    assert ("/approve" in proc.stdout) is approves
    assert ("INDUCT" in proc.stdout) is approves
    assert ("left in PENDING_REVIEW" in proc.stdout) is (not approves)


# ─── Failure counting + exit status ───────────────────────────────────────────


def test_every_warning_line_records_a_failure_or_returns_one() -> None:
    """A ``[!]`` line either goes through fail_step (recorded) or returns 1 to a caller that records it."""
    for n, line in enumerate(_TEXT.splitlines(), 1):
        if "[!]" in line and not line.startswith("fail_step()") and not line.lstrip().startswith("#"):
            assert "return 1" in line, f"onboard-demo.sh:{n} prints [!] without recording a failure: {line.strip()}"
    # The return-1 helpers report to the source loop, which records them.
    assert re.search(r'elif ! approve_and_induct "\$sid" .*; then\n\s+fail_step ', _TEXT), (
        "induction failures must be recorded"
    )


def _finish(tmp_path: Path, failures: list[str]) -> subprocess.CompletedProcess[str]:
    script = f"""set -euo pipefail
{_failure_fns(tmp_path / "failures")}
INDUSTRY=banking NAMESPACE_NAME=ns NS=id COGNITO_USER=u SEED_VIEWER=false METRICS_FILE="" GRANT_COUNT=0
QUESTIONS_FILE="" UI_BUCKET=b
for f in "$@"; do record_failure "$f"; done
{_fn("finish")}
finish
echo "not reached"
"""
    return _bash(script, *failures)


def test_finish_exits_zero_when_nothing_failed(tmp_path: Path) -> None:
    proc = _finish(tmp_path, [])
    assert proc.returncode == 0, proc.stderr
    assert "Demo seed complete." in proc.stdout
    assert "FAILED" not in proc.stderr
    assert "not reached" not in proc.stdout


def test_finish_exits_non_zero_and_lists_the_failures(tmp_path: Path) -> None:
    proc = _finish(tmp_path, ["wh induction incomplete", "m PUT -> HTTP 400"])
    assert proc.returncode == 1
    assert "Demo seed INCOMPLETE." in proc.stdout
    assert "FAILED    : 2 step(s)" in proc.stderr
    assert "- wh induction incomplete" in proc.stderr and "- m PUT -> HTTP 400" in proc.stderr


# ─── Own ontologies ───────────────────────────────────────────────────────────

_IRI = "https://anycompany.example/ontology/fhir"


def _upload(
    tmp_path: Path, listing: list[dict[str, object]], ingest_status: str, upload_code: str = "200"
) -> tuple[str, str, list[str]]:
    """Run upload_ontologies for one manifest entry; returns (ONTOLOGY_FAILED, failures, calls)."""
    (tmp_path / "fhir.ttl").write_text("@prefix x: <http://x/> .\n")
    mf = json.dumps({"ontologies": {"upload": [{"file": "fhir.ttl", "iri": _IRI}]}})
    log = tmp_path / "calls"
    log.write_text("")
    fns = "\n".join(_fn(n) for n in ("mf", "mpath", "fail_ontology", "upload_ontologies"))
    script = f"""set -euo pipefail
NS="{_NS}"; RESP="{tmp_path}/resp"; MF='{mf}'; MANIFEST_DIR="{tmp_path}"; LOG="{log}"
{_failure_fns(tmp_path / "failures")}
sleep() {{ :; }}
api_list() {{ printf '%s' '{json.dumps(listing)}'; }}
api_post() {{
  echo "POST $1" >> "$LOG"
  case "$1" in
    */upload-url) echo '{{"upload_url":"https://s3/p","s3_key":"k1","ontology_id":"{_IRI}"}}' > "$RESP"; echo 200;;
    */ingest-from-s3) echo '{{"job_id":"IJ1"}}' > "$RESP"; echo 202;;
  esac
}}
curl() {{ echo "PUT presigned" >> "$LOG"; echo "{upload_code}"; }}
api_get() {{ echo '{{"status":"{ingest_status}"}}'; }}
{fns}
ONTOLOGY_FAILED=""
upload_ontologies
printf 'failed=[%s]\n' "$ONTOLOGY_FAILED"
"""
    proc = _bash(script)
    assert proc.returncode == 0, proc.stderr
    failed = re.search(r"failed=\[(.*)\]", proc.stdout).group(1).strip()  # type: ignore[union-attr]
    return failed, (tmp_path / "failures").read_text(), log.read_text().splitlines()


@needs_jq
def test_a_completed_ontology_ingest_is_not_a_failure(tmp_path: Path) -> None:
    failed, recorded, calls = _upload(tmp_path, [], "completed")
    assert failed == "" and recorded == ""
    assert any(c.endswith("/ingest-from-s3") for c in calls)


@needs_jq
@pytest.mark.parametrize(("status", "upload_code"), [("failed", "200"), ("running", "200"), ("completed", "403")])
def test_a_failed_ontology_upload_or_ingest_is_recorded(tmp_path: Path, status: str, upload_code: str) -> None:
    failed, recorded, _ = _upload(tmp_path, [], status, upload_code)
    assert failed == _IRI
    assert recorded.startswith(f"{_IRI}: ")


@needs_jq
def test_a_loaded_ontology_is_skipped(tmp_path: Path) -> None:
    listing: list[dict[str, object]] = [{"ontology_id": _IRI, "parse_status": "ok", "status": None}]
    failed, recorded, calls = _upload(tmp_path, listing, "failed")
    assert failed == "" and recorded == "" and calls == []


@needs_jq
def test_a_row_left_by_a_failed_ingest_is_retried(tmp_path: Path) -> None:
    listing: list[dict[str, object]] = [{"ontology_id": _IRI, "parse_status": "parse_error", "status": "failed"}]
    _, _, calls = _upload(tmp_path, listing, "completed")
    assert any(c.endswith("/upload-url") for c in calls), "a failed row is not 'already loaded'"


@needs_jq
@pytest.mark.parametrize(("source", "expect"), [("wh", _IRI), ("lake", ""), ("own", "")])
def test_a_source_grounding_on_a_failed_ontology_is_flagged(source: str, expect: str) -> None:
    mf = json.dumps(
        {
            "induction": {"groundingOntologyIds": ["fibo-agreements", _IRI]},
            "sources": [
                {"name": "wh"},
                {"name": "lake", "induction": {"groundingOntologyIds": ["fibo-agreements"]}},
                {"name": "own", "induction": {"groundingOntologyIds": ["https://anycompany.example/ontology/ok"]}},
            ],
        }
    )
    script = f"""set -euo pipefail
MF='{mf}'; ONTOLOGY_FAILED=" {_IRI}"
{_fn("induction_cfg")}
{_fn("failed_grounding")}
failed_grounding "$1"
"""
    proc = _bash(script, source)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expect


# ─── Grants ───────────────────────────────────────────────────────────────────

_GRANT: dict[str, object] = {"principalType": "Group", "principalId": "all-aws-employees", "role": "data-analyst"}


def _grant(tmp_path: Path, want: dict[str, object], have: list[dict[str, object]]) -> tuple[str, list[str]]:
    log = tmp_path / "calls"
    log.write_text("")
    script = f"""set -euo pipefail
NS="{_NS}"; RESP="{tmp_path}/resp"
{_failure_fns(tmp_path / "failures")}
api_post() {{ echo "POST $1 $2" >> "{log}"; echo 201; }}
{_fn("ensure_grant")}
ensure_grant "$1" "$2" 2>&1
"""
    proc = _bash(script, json.dumps(want), json.dumps(have))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout, log.read_text().splitlines()


@needs_jq
def test_a_missing_grant_is_created(tmp_path: Path) -> None:
    out, calls = _grant(tmp_path, _GRANT, [{**_GRANT, "role": "data-steward"}])
    assert "granted" in out
    assert len(calls) == 1 and calls[0].startswith(f"POST /namespaces/{_NS}/grants ")


@needs_jq
@pytest.mark.parametrize(
    ("want", "have"),
    [
        ({}, {}),
        ({"tableAllowlist": ["a", "b"]}, {"tableAllowlist": ["b", "a"]}),
        ({"columnDenylist": {"t": ["x", "y"]}}, {"columnDenylist": {"t": ["y", "x"]}}),
    ],
    ids=["unscoped", "list-order", "denylist-order"],
)
def test_an_existing_grant_with_the_same_scope_is_left_quietly(
    tmp_path: Path, want: dict[str, object], have: dict[str, object]
) -> None:
    out, calls = _grant(tmp_path, {**_GRANT, **want}, [{**_GRANT, "grantId": "g1", **have}])
    assert out.strip().endswith(": exists") and "WARN" not in out
    assert calls == []


@needs_jq
@pytest.mark.parametrize(
    ("want", "have"),
    [
        ({"columnDenylist": {"account": ["ssn"]}}, {}),
        ({}, {"tableAllowlist": []}),
        ({"allowedMetrics": ["m1"]}, {"allowedMetrics": ["m2"]}),
    ],
    ids=["denylist-added", "deny-all-vs-unrestricted", "metrics-differ"],
)
def test_an_existing_grant_with_a_different_scope_warns_and_is_kept(
    tmp_path: Path, want: dict[str, object], have: dict[str, object]
) -> None:
    out, calls = _grant(tmp_path, {**_GRANT, **want}, [{**_GRANT, "grantId": "g1", **have}])
    assert "WARN: Group all-aws-employees -> data-analyst exists with a different scope" in out
    assert calls == [], "the existing grant is kept, not re-posted"
    assert (tmp_path / "failures").read_text() == "", "a scope difference warns; it does not fail the run"


@needs_jq
def test_an_unreadable_proposal_listing_does_not_re_induce() -> None:
    script = f"""set -euo pipefail
NS="{_NS}"; FORCE_INDUCE=false; MF='{{}}'
{_fn("mf")}
api_get() {{ echo '{{"message":"Forbidden"}}'; }}
{_fn("list_all_proposals")}
{_fn("source_already_inducted")}
source_status() {{ echo PENDING_REVIEW; }}
steward_edits() {{ :; }}
api_post() {{ echo "POST $1" >&2; }}
induct_source() {{ echo INDUCT; }}
{_fn("approve_and_induct")}
rc=0; approve_and_induct sid1 sfx wh 2>&1 || rc=$?
echo "rc=$rc"
"""
    proc = _bash(script)
    assert proc.returncode == 0, proc.stderr
    assert "rc=1" in proc.stdout and "could not list proposals" in proc.stdout
    assert "/approve" not in proc.stdout and "INDUCT" not in proc.stdout


# ─── Docs bucket ──────────────────────────────────────────────────────────────

_ACCT = "445567103702"


def _bucket(tmp_path: Path, exists: bool, policy: str | None, fail: str = "") -> tuple[int, list[list[str]], str, str]:
    """Run ensure_docs_bucket against a stub aws; returns (rc, aws calls, failures, stderr).

    *policy* is the current bucket policy (None: NoSuchBucketPolicy); *fail* names an
    s3api operation that errors (AccessDenied).
    """
    log = tmp_path / "aws"
    log.write_text("")
    pol = tmp_path / "policy"
    if policy is not None:
        pol.write_text(policy)
    script = f"""set -euo pipefail
WORK_DIR="{tmp_path}"; REGION=us-west-2; ACCOUNT={_ACCT}; PREFIX=coa; INDUSTRY=banking
{_failure_fns(tmp_path / "failures")}
aws_stub() {{
  jq -nc '$ARGS.positional' --args -- "$@" >> "{log}"
  local op="$2"
  [[ "$op" == "{fail}" ]] && {{ echo "An error occurred (AccessDenied) when calling $op" >&2; return 254; }}
  case "$op" in
    head-bucket) {"return 0" if exists else "return 254"};;
    get-bucket-policy)
      [[ -f "{pol}" ]] || {{ echo "An error occurred (NoSuchBucketPolicy)" >&2; return 254; }}
      cat "{pol}";;
  esac
}}
AWS=(aws_stub)
{_fn("ensure_docs_bucket")}
rc=0; (ensure_docs_bucket coa-demo-docs-banking) || rc=$?
echo "rc=$rc"
"""
    proc = _bash(script)
    assert proc.returncode == 0, proc.stderr
    rc = int(re.search(r"rc=(\d+)", proc.stdout).group(1))  # type: ignore[union-attr]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return rc, calls, (tmp_path / "failures").read_text(), proc.stderr


def _ops(calls: list[list[str]]) -> list[str]:
    return [c[1] for c in calls]


def _put_policy(calls: list[list[str]]) -> dict[str, Any]:
    (call,) = [c for c in calls if c[1] == "put-bucket-policy"]
    policy: dict[str, Any] = json.loads(call[call.index("--policy") + 1])
    return policy


@needs_jq
def test_a_new_docs_bucket_is_created_and_hardened(tmp_path: Path) -> None:
    rc, calls, recorded, _ = _bucket(tmp_path, exists=False, policy=None)
    assert rc == 0 and recorded == ""
    assert _ops(calls) == [
        "head-bucket",
        "create-bucket",
        "put-bucket-encryption",
        "put-bucket-tagging",
        "put-public-access-block",
        "get-bucket-policy",
        "put-bucket-policy",
    ]
    assert [s["Sid"] for s in _put_policy(calls)["Statement"]] == ["DenyInsecureTransport"]
    for c in calls:
        owned = "--expected-bucket-owner" in c
        assert owned is (c[1] != "create-bucket"), c  # CreateBucket takes no ExpectedBucketOwner
        if owned:
            assert c[c.index("--expected-bucket-owner") + 1] == _ACCT


@needs_jq
def test_an_existing_bucket_gets_the_missing_tls_statement_and_keeps_its_others(tmp_path: Path) -> None:
    other = {
        "Sid": "AllowReader",
        "Effect": "Allow",
        "Principal": {"AWS": "arn:aws:iam::1:root"},
        "Action": "s3:GetObject",
    }
    rc, calls, recorded, _ = _bucket(
        tmp_path, exists=True, policy=json.dumps({"Version": "2012-10-17", "Statement": other})
    )
    assert rc == 0 and recorded == ""
    assert "create-bucket" not in _ops(calls) and "put-bucket-tagging" not in _ops(calls), "tags are not replaced"
    assert "put-public-access-block" in _ops(calls)
    assert [s["Sid"] for s in _put_policy(calls)["Statement"]] == ["AllowReader", "DenyInsecureTransport"]


@needs_jq
def test_an_already_hardened_bucket_is_left_as_is(tmp_path: Path) -> None:
    _, first, _, _ = _bucket(tmp_path, exists=False, policy=None)
    policy = json.dumps(_put_policy(first))
    rc, calls, recorded, _ = _bucket(tmp_path, exists=True, policy=policy)
    assert rc == 0 and recorded == ""
    assert _ops(calls) == ["head-bucket", "put-public-access-block", "get-bucket-policy"]


@needs_jq
@pytest.mark.parametrize("op", ["put-public-access-block", "get-bucket-policy", "put-bucket-policy"])
def test_a_failed_re_assert_on_an_existing_bucket_is_recorded(tmp_path: Path, op: str) -> None:
    rc, _, recorded, _ = _bucket(tmp_path, exists=True, policy=None, fail=op)
    assert rc == 0, "an existing (earlier-hardened) bucket does not stop the run"
    assert recorded.startswith("docs bucket coa-demo-docs-banking: ")


@needs_jq
def test_a_failed_hardening_right_after_create_is_fatal(tmp_path: Path) -> None:
    rc, _, _, stderr = _bucket(tmp_path, exists=False, policy=None, fail="put-public-access-block")
    assert rc == 1
    assert "ERROR: docs bucket coa-demo-docs-banking: could not block public access" in stderr
