# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Maven check (``check-maven.sh``) and where it runs.

The connector deploy scripts build a jar with Maven, and make deploy-dev runs one late
in the deploy, so a missing ``mvn``, or one running on an unsupported JDK, must stop
them before they start. The platform preflight never runs Maven and must not require
it. Each test runs the scripts against a stub ``mvn`` and a stub JDK.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check-maven.sh"


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)


def _stub_jdk(tmp_path: Path, banner: str) -> Path:
    jdk = tmp_path / "jdk"
    _stub(jdk / "bin" / "java", f"echo '{banner}' >&2")
    return jdk


def _stub_mvn(tmp_path: Path, jdk: Path | str, *, exit_code: int = 0, version: str = "3.9.16") -> Path:
    bin_dir = tmp_path / "bin"
    _stub(
        bin_dir / "mvn",
        f"""cat <<'EOF'
Apache Maven {version} (2bdd9fddda4b155ebf8000e807eb73fd829a51d5)
Maven home: /opt/maven
Java version: 21.0.13, vendor: Amazon.com Inc., runtime: {jdk}
OS name: "mac os x", version: "15.0", arch: "aarch64", family: "mac"
EOF
exit {exit_code}""",
    )
    return bin_dir


def _run(bin_dir: Path, *, path_java: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run with only the stubs and coreutils on PATH, plus ``path_java``'s bin if given."""
    search = [str(bin_dir)]
    if path_java is not None:
        search.append(str(path_java / "bin"))
    search += ["/usr/bin", "/bin"]
    return subprocess.run(
        ["/bin/bash", str(_SCRIPT)],
        cwd=_ROOT,
        env={**os.environ, "PATH": os.pathsep.join(search)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_maven_on_a_supported_jdk_passes(tmp_path: Path) -> None:
    jdk = _stub_jdk(tmp_path, 'openjdk version "21.0.13" 2026-10-21 LTS')

    result = _run(_stub_mvn(tmp_path, jdk))

    assert result.returncode == 0
    assert result.stdout.strip() == "Maven 3.9.16 found, running on Java 21 (version 21.0.13)"


def test_missing_maven_fails_with_an_install_hint(tmp_path: Path) -> None:
    empty = tmp_path / "bin"
    empty.mkdir()

    result = _run(empty)

    assert result.returncode == 1
    assert "Maven (mvn) not found" in result.stderr
    assert "connector jar" in result.stderr
    assert "mise install" in result.stderr


def test_maven_on_jdk_25_fails_even_when_path_java_is_supported(tmp_path: Path) -> None:
    # brew's mvn runs on the newest OpenJDK whatever `java` on PATH is; the connector
    # build's enforcer rule would stop it ~30 minutes into a fresh deploy.
    maven_jdk = _stub_jdk(tmp_path / "maven", 'openjdk version "25.0.1" 2025-10-21 LTS')
    path_jdk = _stub_jdk(tmp_path / "path", 'openjdk version "21.0.13" 2026-10-21 LTS')

    result = _run(_stub_mvn(tmp_path, maven_jdk), path_java=path_jdk)

    assert result.returncode == 1
    mvn = tmp_path / "bin" / "mvn"
    assert f"Maven 3.9.16 ({mvn}) runs on {maven_jdk}: Java 25 is not supported" in result.stderr
    assert "Java 17-24 required, 21 recommended" in result.stderr
    # mise install alone can't fix another mvn first on PATH, so the hint names the alternatives.
    assert "Set JAVA_HOME to a JDK 17-24, or use mise's Maven and JDK" in result.stderr
    assert "mise exec -- make <target>" in result.stderr
    assert "Run: mise install, or point JAVA_HOME" not in result.stderr


def test_maven_on_a_too_old_jdk_fails(tmp_path: Path) -> None:
    jdk = _stub_jdk(tmp_path, 'openjdk version "11.0.25" 2024-10-15')

    result = _run(_stub_mvn(tmp_path, jdk))

    assert result.returncode == 1
    assert "Java 17-24 required (found 11" in result.stderr


def test_failing_mvn_reports_its_first_line(tmp_path: Path) -> None:
    jdk = _stub_jdk(tmp_path, 'openjdk version "21.0.13" 2026-10-21 LTS')

    result = _run(_stub_mvn(tmp_path, jdk, exit_code=1))

    assert result.returncode == 1
    assert f"Could not run '{tmp_path / 'bin' / 'mvn'} -v': Apache Maven 3.9.16" in result.stderr


def test_failing_mvn_with_no_output_says_so(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _stub(bin_dir / "mvn", "exit 1")

    result = _run(bin_dir)

    assert result.returncode == 1
    assert f"Could not run '{bin_dir / 'mvn'} -v': it printed nothing." in result.stderr


@pytest.mark.parametrize("version", ["3.6.2", "3.5.4", "3.3.9"])
def test_maven_below_the_connector_builds_minimum_fails(tmp_path: Path, version: str) -> None:
    jdk = _stub_jdk(tmp_path, 'openjdk version "21.0.13" 2026-10-21 LTS')

    result = _run(_stub_mvn(tmp_path, jdk, version=version))

    assert result.returncode == 1
    assert f"Maven {version} ({tmp_path / 'bin' / 'mvn'}) is too old" in result.stderr
    assert "needs Maven 3.6.3 or newer" in result.stderr


@pytest.mark.parametrize("version", ["3.6.3", "3.10.0", "4.0.0-rc-4"])
def test_maven_at_or_above_the_minimum_passes(tmp_path: Path, version: str) -> None:
    jdk = _stub_jdk(tmp_path, 'openjdk version "21.0.13" 2026-10-21 LTS')

    result = _run(_stub_mvn(tmp_path, jdk, version=version))

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(f"Maven {version} found")


def test_minimum_maven_matches_the_docs() -> None:
    minimum = re.search(r'^MIN_MAVEN="([^"]+)"', _SCRIPT.read_text(), re.MULTILINE)
    assert minimum, "check-maven.sh sets MIN_MAVEN"
    deploying = (_ROOT / "external-docs" / "content" / "deploying.md").read_text()
    assert f"| Maven | {minimum.group(1)}+ " in deploying


def test_failing_mvn_skips_the_java_tool_options_banner(tmp_path: Path) -> None:
    """The JVM prints "Picked up JAVA_TOOL_OPTIONS" first; report the real error instead."""
    bin_dir = tmp_path / "bin"
    banner = "Picked up JAVA_TOOL_OPTIONS: -Dhttps.proxyHost=proxy"
    _stub(bin_dir / "mvn", f"printf '{banner}\\nError: invalid MAVEN_OPTS\\n'; exit 1")

    result = _run(bin_dir)

    assert result.returncode == 1
    assert "-v': Error: invalid MAVEN_OPTS." in result.stderr
    assert "Picked up" not in result.stderr


def test_unparseable_mvn_output_only_warns(tmp_path: Path) -> None:
    """A Maven whose `mvn -v` we can't read may still build: warn, don't block."""
    result = _run(_stub_mvn(tmp_path, tmp_path / "no-such-jdk"))

    assert result.returncode == 0
    assert "WARN: Maven 3.9.16 found" in result.stderr
    assert "could not tell which JDK it runs on" in result.stderr


def _path_without_mvn(tmp_path: Path) -> list[str]:
    """The caller's PATH, each directory holding an ``mvn`` replaced by a copy of it without one."""
    dirs = []
    for i, entry in enumerate(d for d in os.environ["PATH"].split(os.pathsep) if d):
        if not (Path(entry) / "mvn").exists():
            dirs.append(entry)
            continue
        shadow = tmp_path / f"no-mvn-{i}"
        shadow.mkdir()
        for tool in Path(entry).iterdir():
            if tool.name != "mvn":
                (shadow / tool.name).symlink_to(tool)
        dirs.append(str(shadow))
    return dirs


def test_platform_preflight_does_not_need_maven(tmp_path: Path) -> None:
    """make preflight and a platform-only deploy.sh never run Maven."""
    bin_dir = tmp_path / "bin"
    # Nothing leaves the machine: every aws call fails as if signed out, and the
    # Smithy step's `make generate` is a no-op.
    _stub(bin_dir / "aws", "echo 'Unable to locate credentials' >&2; exit 255")
    _stub(bin_dir / "make", "exit 0")
    path = os.pathsep.join([str(bin_dir), *_path_without_mvn(tmp_path)])
    assert shutil.which("mvn", path=path) is None, "precondition: no mvn on PATH"

    result = subprocess.run(
        ["/bin/bash", str(_ROOT / "scripts" / "preflight-deploy.sh"), "dev"],
        cwd=_ROOT,
        env={**os.environ, "PATH": path, "AWS_REGION": "us-east-1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    output = result.stdout + result.stderr
    assert "Pre-deploy preflight checks" in output  # it ran
    assert "Maven" not in output
    assert "mvn" not in output


@pytest.mark.parametrize(
    "script", ["deploy-all.sh", "deploy-managed-databricks-connector.sh", "deploy-example-connector.sh"]
)
def test_maven_check_stops_the_scripts_that_build_with_it(tmp_path: Path, script: str) -> None:
    bin_dir = tmp_path / "bin"
    _stub(bin_dir / "mvn", "echo 'The JAVA_HOME environment variable is not defined correctly'; exit 1")
    # Every SSM read returns a value, so the connector scripts get as far as their build.
    _stub(bin_dir / "aws", f'echo "aws $*" >>"{tmp_path}/aws.log"; echo stub-value')
    _stub(bin_dir / "pnpm", f'echo "pnpm $*" >>"{tmp_path}/pnpm.log"')
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "AWS_REGION": "us-east-1"}
    for var in ("SCL_PREFIX", "FUNCTION_NAME_PREFIX", "DATABRICKS_WORKSPACE_HOSTNAME", "CREDENTIAL_SECRET_ARN"):
        env.pop(var, None)

    result = subprocess.run(
        ["bash", str(_ROOT / "scripts" / script), "dev"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert f"ERROR: Could not run '{bin_dir / 'mvn'} -v': The JAVA_HOME environment variable" in result.stderr
    assert not (tmp_path / "pnpm.log").exists()
    if script == "deploy-all.sh":
        # Before the platform deploy starts, not when the connector build is reached.
        assert not (tmp_path / "aws.log").exists()


def _run_connector_script(tmp_path: Path, script: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    _stub(bin_dir / "aws", f'echo "aws $*" >>"{tmp_path}/aws.log"; echo stub-value')
    _stub(bin_dir / "pnpm", f'echo "pnpm $*" >>"{tmp_path}/pnpm.log"')
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "AWS_REGION": "us-east-1"}
    for var in ("SCL_PREFIX", "FUNCTION_NAME_PREFIX", "DATABRICKS_WORKSPACE_HOSTNAME", "CREDENTIAL_SECRET_ARN"):
        env.pop(var, None)

    return subprocess.run(
        ["bash", str(_ROOT / "scripts" / script), "dev"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


_CONNECTOR_SCRIPTS = ["deploy-managed-databricks-connector.sh", "deploy-example-connector.sh"]


@pytest.mark.parametrize("script", _CONNECTOR_SCRIPTS)
def test_pre_3_6_3_maven_stops_the_connector_build(tmp_path: Path, script: str) -> None:
    """Pre-3.5 Maven prints "Java home:" instead of "runtime:", but its version line is enough to stop it."""
    _stub(tmp_path / "bin" / "mvn", "printf 'Apache Maven 3.3.9\\nJava home: /opt/jdk/jre\\n'")

    result = _run_connector_script(tmp_path, script)

    assert result.returncode == 1
    assert "ERROR: Maven 3.3.9" in result.stderr
    assert "needs Maven 3.6.3 or newer" in result.stderr
    assert not (tmp_path / "pnpm.log").exists()


@pytest.mark.parametrize("script", _CONNECTOR_SCRIPTS)
def test_unreadable_mvn_version_output_warns_and_the_build_goes_on(tmp_path: Path, script: str) -> None:
    """A `mvn -v` with no version line we can read may still build: warn, then build anyway."""
    _stub(tmp_path / "bin" / "mvn", "printf 'Maven home: /opt/maven\\nJava home: /opt/jdk/jre\\n'")

    result = _run_connector_script(tmp_path, script)

    assert "WARN: Maven ? found" in result.stderr
    assert "skipping the JDK check" in result.stderr
    # The JDK check never ran, and nothing stopped the script before the build.
    assert "is not supported" not in result.stderr
    assert "ERROR:" not in result.stderr
    assert (tmp_path / "pnpm.log").exists()
