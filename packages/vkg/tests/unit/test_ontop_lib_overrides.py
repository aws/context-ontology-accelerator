# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ontop-lib-overrides.sh and its manifest.

The script runs inside the image build to swap the Ontop CLI's bundled JARs for
patched versions. These tests execute the REAL script against a fake Ontop lib
directory and a local file:// "Maven repository", so a regression in its
delete-then-install logic, its sha256 check or its no-downgrade guard fails here
rather than as a vulnerable or broken image.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_VKG = Path(__file__).resolve().parents[2]
_SCRIPT = _VKG / "ontop-lib-overrides.sh"
_MANIFEST = _VKG / "ontop-lib-overrides.txt"

_needs_tools = pytest.mark.skipif(
    not (shutil.which("sh") and shutil.which("curl") and shutil.which("sha256sum")),
    reason="needs sh, curl and sha256sum",
)


def _entries(text: str) -> list[list[str]]:
    return [line.split() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _publish(repo: Path, path: str, content: bytes) -> str:
    """Write a JAR into the fake Maven repo and return its sha256."""
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _run(manifest: Path, lib: Path, repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(_SCRIPT), str(manifest), str(lib)],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "MAVEN_BASE": repo.as_uri()},
        capture_output=True,
        text=True,
        check=False,
    )


class TestManifest:
    def test_every_entry_is_a_maven_path_with_a_sha256(self):
        entries = _entries(_MANIFEST.read_text())
        assert entries, "manifest has no entries"
        for entry in entries:
            assert len(entry) == 2, entry
            path, sha256 = entry
            parts = path.split("/")
            artifact, version, jar = parts[-3], parts[-2], parts[-1]
            assert jar == f"{artifact}-{version}.jar", path
            assert re.fullmatch(r"[0-9a-f]{64}", sha256), path
            # The script finds bundled and installed JARs with `<artifactId>-[0-9]*.jar`.
            assert version[:1].isdigit(), path
            assert fnmatch.fnmatch(jar, f"{artifact}-[0-9]*.jar"), path

    def test_no_entry_glob_matches_another_entrys_jar(self):
        # rm -f "<artifactId>-[0-9]*.jar" must only ever delete that artifact.
        jars = {path.split("/")[-3]: path.split("/")[-1] for path, _ in _entries(_MANIFEST.read_text())}
        for artifact in jars:
            for other, jar in jars.items():
                if other != artifact:
                    assert not fnmatch.fnmatch(jar, f"{artifact}-[0-9]*.jar"), (artifact, jar)

    def test_each_artifact_appears_once(self):
        artifacts = [path.split("/")[-3] for path, _ in _entries(_MANIFEST.read_text())]
        assert len(artifacts) == len(set(artifacts))


@_needs_tools
class TestScript:
    def _layout(self, tmp_path: Path) -> tuple[Path, Path]:
        lib = tmp_path / "lib"
        lib.mkdir()
        # What the Ontop 5.5.0 zip ships, including neighbours whose names share a
        # prefix with an overridden artifact and must survive.
        for jar in (
            "thymeleaf-3.0.15.RELEASE.jar",
            "thymeleaf-spring5-3.0.15.RELEASE.jar",
            "thymeleaf-extras-java8time-3.0.4.RELEASE.jar",
            "spring-web-5.3.31.jar",
            "spring-webmvc-5.3.31.jar",
            "ontop-model-5.5.0.jar",
        ):
            (lib / jar).write_bytes(b"old")
        return lib, tmp_path / "repo"

    def test_replaces_only_the_named_artifact(self, tmp_path):
        lib, repo = self._layout(tmp_path)
        path = "org/thymeleaf/thymeleaf/3.1.5.RELEASE/thymeleaf-3.1.5.RELEASE.jar"
        sha = _publish(repo, path, b"patched")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(f"# comment\n\n{path} {sha}\n")

        result = _run(manifest, lib, repo)

        assert result.returncode == 0, result.stderr
        names = sorted(p.name for p in lib.iterdir())
        assert "thymeleaf-3.0.15.RELEASE.jar" not in names
        assert (lib / "thymeleaf-3.1.5.RELEASE.jar").read_bytes() == b"patched"
        # Prefix-sharing artifacts are untouched; java8time is removed outright.
        assert "thymeleaf-spring5-3.0.15.RELEASE.jar" in names
        assert "thymeleaf-extras-java8time-3.0.4.RELEASE.jar" not in names
        assert "ontop-model-5.5.0.jar" in names

    def test_spring_web_does_not_remove_spring_webmvc(self, tmp_path):
        lib, repo = self._layout(tmp_path)
        path = "org/springframework/spring-web/5.3.39/spring-web-5.3.39.jar"
        sha = _publish(repo, path, b"patched")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(f"{path} {sha}\n")

        result = _run(manifest, lib, repo)

        assert result.returncode == 0, result.stderr
        assert not (lib / "spring-web-5.3.31.jar").exists()
        assert (lib / "spring-web-5.3.39.jar").exists()
        assert (lib / "spring-webmvc-5.3.31.jar").exists()

    def test_sha256_mismatch_fails_and_stops_before_later_entries(self, tmp_path):
        lib, repo = self._layout(tmp_path)
        bad = "org/thymeleaf/thymeleaf/3.1.5.RELEASE/thymeleaf-3.1.5.RELEASE.jar"
        _publish(repo, bad, b"tampered")
        good = "org/springframework/spring-web/5.3.39/spring-web-5.3.39.jar"
        good_sha = _publish(repo, good, b"patched")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(f"{bad} {'0' * 64}\n{good} {good_sha}\n")

        result = _run(manifest, lib, repo)

        assert result.returncode != 0
        assert f"sha256 mismatch for {bad}" in result.stderr
        # The unverified JAR never reaches lib/, the bundled one is left in place,
        # and the run stops before installing the next entry.
        assert not any(p.name.startswith("thymeleaf-3.1.5") for p in lib.iterdir())
        assert (lib / "thymeleaf-3.0.15.RELEASE.jar").exists()
        assert not (lib / "spring-web-5.3.39.jar").exists()
        assert (lib / "spring-web-5.3.31.jar").exists()

    def test_missing_artifact_fails(self, tmp_path):
        lib, repo = self._layout(tmp_path)
        repo.mkdir()
        path = "org/thymeleaf/thymeleaf/3.1.5.RELEASE/thymeleaf-3.1.5.RELEASE.jar"
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(f"{path} {'0' * 64}\n")

        assert _run(manifest, lib, repo).returncode != 0

    def test_refuses_to_downgrade_a_newer_bundled_jar(self, tmp_path):
        # A future Ontop release bundling a newer JAR than the pin must fail the
        # build rather than be reverted to the older pinned version.
        lib, repo = self._layout(tmp_path)
        (lib / "spring-web-5.3.31.jar").unlink()
        (lib / "spring-web-5.3.40.jar").write_bytes(b"newer")
        path = "org/springframework/spring-web/5.3.39/spring-web-5.3.39.jar"
        sha = _publish(repo, path, b"patched")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(f"{path} {sha}\n")

        result = _run(manifest, lib, repo)

        assert result.returncode != 0
        assert "newer than the pinned" in result.stderr
        assert (lib / "spring-web-5.3.40.jar").read_bytes() == b"newer"

    def test_manifest_versions_are_newer_than_ontop_550_bundle(self, tmp_path):
        # The real manifest against the JAR versions Ontop 5.5.0 ships: every
        # entry is an upgrade, so the downgrade guard must not trip.
        bundled = {
            "tomcat-embed-core": "9.0.111",
            "tomcat-embed-el": "9.0.83",
            "tomcat-embed-websocket": "9.0.83",
            "jackson-annotations": "2.15.4",
            "jackson-core": "2.15.4",
            "jackson-databind": "2.15.4",
            "jackson-datatype-guava": "2.15.4",
            "jackson-datatype-jdk8": "2.15.4",
            "jackson-datatype-jsr310": "2.13.5",
            "jackson-module-parameter-names": "2.13.5",
            "spring-aop": "5.3.31",
            "spring-beans": "5.3.31",
            "spring-context": "5.3.31",
            "spring-core": "5.3.31",
            "spring-expression": "5.3.31",
            "spring-jcl": "5.3.31",
            "spring-web": "5.3.31",
            "spring-webmvc": "5.3.31",
            "thymeleaf": "3.0.15.RELEASE",
            "thymeleaf-spring5": "3.0.15.RELEASE",
            "attoparser": "2.0.5.RELEASE",
            "commons-beanutils": "1.9.4",
            "guava": "32.0.1-jre",
            "failureaccess": "1.0.1",
        }
        lib = tmp_path / "lib"
        lib.mkdir()
        repo = tmp_path / "repo"
        lines = []
        for path, _ in _entries(_MANIFEST.read_text()):
            artifact = path.split("/")[-3]
            assert artifact in bundled, f"{artifact} is not in the Ontop 5.5.0 bundle list"
            (lib / f"{artifact}-{bundled[artifact]}.jar").write_bytes(b"old")
            lines.append(f"{path} {_publish(repo, path, artifact.encode())}")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text("\n".join(lines) + "\n")

        result = _run(manifest, lib, repo)

        assert result.returncode == 0, result.stderr
        assert len(list(lib.iterdir())) == len(bundled)
