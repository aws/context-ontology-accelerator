# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Assert that paths referenced in README and Makefile actually exist.

Catches the class of bug where the mirror strips a directory but leaves its
consumers (Makefile targets, README structure listings) intact — making the
product look broken rather than intentionally trimmed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_readme_structure_paths_exist() -> None:
    """Every top-level directory listed in README's Repository Structure must exist."""
    readme = _REPO_ROOT / "README.md"
    content = readme.read_text()

    # Extract the fenced code block under "## Repository Structure"
    match = re.search(
        r"## Repository Structure\s*```[^\n]*\n(.*?)```",
        content,
        re.DOTALL,
    )
    assert match, "README.md must contain a '## Repository Structure' fenced block"

    # Parse ONLY top-level tree lines (no leading │ indent) like:
    #   ├── models/   # comment
    # Skip nested entries (those indented with │) since they're relative to their parent.
    top_level_pattern = re.compile(r"^[├└]── ([a-zA-Z][a-zA-Z0-9_./-]+?)/\s", re.MULTILINE)
    paths = top_level_pattern.findall(match.group(1))
    assert paths, "Should find at least one top-level path in the structure block"

    # Directories that are build outputs (generated, not checked in) are
    # legitimately listed in the structure but absent until `make generate` runs.
    _GENERATED_DIRS = {"smithy-generated"}

    missing = [p for p in paths if p not in _GENERATED_DIRS and not (_REPO_ROOT / p).exists()]
    assert not missing, (
        f"README Repository Structure lists paths that do not exist: {missing}. "
        f"Either add the directories or remove them from the listing."
    )


@pytest.mark.parametrize(
    "target",
    [
        "docs",
        "test-integ",
        "load-test",
        "load-test-slow",
        "load-test-teardown",
    ],
)
def test_guarded_makefile_targets_do_not_fail_on_missing_dirs(target: str) -> None:
    """Makefile targets that reference potentially-stripped dirs must be guarded."""
    makefile = _REPO_ROOT / "Makefile"
    content = makefile.read_text()

    # Find the recipe lines for this target
    pattern = re.compile(rf"^{re.escape(target)}:.*?(?=^\S|\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(content)
    assert match, f"Makefile target '{target}' not found"

    recipe = match.group(0)
    # The recipe must contain a guard (if/test/[) rather than unconditional execution
    assert re.search(r"\bif\b|\btest\b|\[", recipe), (
        f"Makefile target '{target}' executes unconditionally but references a "
        f"directory that may be stripped from the public mirror. Wrap it in a guard."
    )


# `tests/unit/` is mirrored publicly; every other `tests/` subdirectory is not (see
# the allowlist in .npmignore). Matches the `_REPO_ROOT / "tests" / "cdk"` idiom, plus
# the shorter `Path(__file__).parents[1] / "integ"` form that names no "tests" segment.
_UNMIRRORED_TESTS_REF = re.compile(r'"tests"\s*/\s*"(?!unit)|/\s*"(?:integ|cdk)"')


def _mirrored_unit_test_files() -> list[Path]:
    """Every unit test file that ships to the public mirror.

    Not just the repo-root suite: `.npmignore` re-includes `/packages/` and `/libs/`
    wholesale, so each package's own `tests/unit/` ships too, nested directories
    included, and each is just as capable of reaching into that package's stripped
    `tests/integ/` at import time.
    """
    files = list((_REPO_ROOT / "tests" / "unit").glob("*.py"))
    for unit_dir in sorted(
        [*(_REPO_ROOT / "packages").glob("*/tests/unit"), *(_REPO_ROOT / "libs").glob("*/tests/unit")]
    ):
        files.extend(unit_dir.rglob("*.py"))
    return files


def test_unit_tests_reaching_outside_tests_unit_skip_when_the_path_is_absent() -> None:
    """A mirrored unit test must not hard-require an unmirrored fixture.

    The public mirror ships `tests/unit/` without `tests/cdk/` (or, per-package,
    without that package's `tests/integ/`). A test that reads from an unmirrored
    path at import time turns into a pytest *collection* error there, which aborts
    the entire suite (exit 2, zero tests run) rather than failing just itself. The
    fix is always a module-level skip, so require one.
    """
    offenders = []
    for path in sorted(_mirrored_unit_test_files()):
        if path.name == Path(__file__).name:
            continue  # this file only names the idiom in a comment
        source = path.read_text()
        if _UNMIRRORED_TESTS_REF.search(source) and "allow_module_level=True" not in source:
            offenders.append(str(path.relative_to(_REPO_ROOT)))

    assert not offenders, (
        f"{offenders} reference a tests/ subdirectory that is stripped from the public "
        "mirror without a module-level skip. Add: "
        "if not _SCRIPT.exists(): pytest.skip(..., allow_module_level=True)"
    )


# `ci/` is not mirrored either. Matches a quoted "ci" or "ci/..." path part (`_REPO_ROOT / "ci"`, `joinpath("ci", ...)`,
# `os.path.join(ROOT, 'ci')`) and an f-string `{ROOT}/ci/`.
_UNMIRRORED_CI_REF = re.compile(r"""["']ci["'/]|\}/ci/""")


def test_unit_tests_reading_ci_check_that_it_is_present() -> None:
    """A mirrored unit test that reads `ci/` must skip, or leave those files out, where the mirror lacks it.

    The GitHub PR workflow runs `pytest tests/unit` on the mirror, so a bare read of `ci/merge-request.yml` there is
    a FileNotFoundError, not a skip. Accept a module-level skip or a `_CI_DIR.is_dir()` check.
    """
    offenders = []
    for path in sorted(_mirrored_unit_test_files()):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text()
        if _UNMIRRORED_CI_REF.search(source) and not (
            "allow_module_level=True" in source or "_CI_DIR.is_dir()" in source
        ):
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, f"{offenders} read ci/, which the public mirror does not ship, without checking for it"


@pytest.mark.parametrize(
    "source",
    [
        '_REPO_ROOT / "ci" / "mainline.yml"',
        "_REPO_ROOT / 'ci'",
        'ROOT.joinpath("ci", "merge-request.yml")',
        'os.path.join(ROOT, "ci", "pages.yml")',
        "Path('ci/merge-request.yml')",
        'f"{ROOT}/ci/merge-request.yml"',
    ],
)
def test_the_ci_check_sees_every_spelling_of_a_ci_path(source: str) -> None:
    assert _UNMIRRORED_CI_REF.search(source), source


def test_the_mirror_checks_scan_nested_and_library_unit_tests() -> None:
    """`/packages/` and `/libs/` ship wholesale, so every `tests/unit/` file under them is mirrored, nested ones too."""
    scanned = {str(path.relative_to(_REPO_ROOT)) for path in _mirrored_unit_test_files()}
    expected = {
        str(path.relative_to(_REPO_ROOT))
        for pattern in ("packages/*/tests/unit/**/*.py", "libs/*/tests/unit/**/*.py")
        for path in _REPO_ROOT.glob(pattern)
    }
    assert any(name.startswith("libs/") for name in expected)
    assert expected <= scanned, sorted(expected - scanned)


# Directories and files that ship to the public mirror and carry install instructions.
_MIRRORED_INSTRUCTION_SOURCES = ("README.md", "docs", "external-docs", "scripts")


def _mirrored_files_mentioning(needle: str) -> list[str]:
    """Mirrored docs and scripts containing `needle`, as repo-relative paths."""
    hits = []
    for entry in _MIRRORED_INSTRUCTION_SOURCES:
        root = _REPO_ROOT / entry
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in candidates:
            if not path.is_file():
                continue
            try:
                if needle in path.read_text():
                    hits.append(str(path.relative_to(_REPO_ROOT)))
            except UnicodeDecodeError:
                continue
    return hits


def _npmignore() -> Path:
    """The root .npmignore. `npm pack` never ships it, so only the mirror (which also lacks ci/) skips without it."""
    path = _REPO_ROOT / ".npmignore"
    if not path.is_file():
        if (_REPO_ROOT / "ci").is_dir():
            pytest.fail(".npmignore is missing; the mirror allowlist checks below need it")
        pytest.skip(".npmignore is not shipped to the public mirror; its allowlist is checked in the source repo")
    return path


def test_mise_config_ships_if_any_mirrored_file_says_to_run_mise_install() -> None:
    """`mise install` without a mirrored .mise.toml installs nothing, silently.

    mise reads its tool versions from .mise.toml. The root .npmignore is a
    fail-closed allowlist, so a config file is absent from the mirror unless it is
    named there. Ship the instruction without the config and the reader's
    `mise install` is a no-op, after which scripts/preflight-deploy.sh fails on a
    missing Node and points them back at the command that did nothing.
    """
    callers = _mirrored_files_mentioning("mise install")
    if not callers:
        pytest.skip("no mirrored file instructs `mise install`")

    npmignore = _npmignore().read_text()
    assert re.search(r"^!/\.mise\.toml$", npmignore, re.MULTILINE), (
        f"{callers} instruct `mise install`, but .npmignore does not re-include "
        "/.mise.toml, so the public mirror ships the instruction without the file "
        "it reads. Add '!/.mise.toml' to the root config allowlist."
    )


# Host build output that must never reach a Docker build context. `*.egg-info` is the
# one with teeth: setuptools writes it under an allowlisted `src/`, so its presence
# changes the asset hash of every image built from the repo root.
_HOST_BUILD_OUTPUT = ("**/*.egg-info", "**/.venv", "**/.ruff_cache", "**/.mypy_cache")


def _ignore_entries(path: Path) -> list[str]:
    """Non-comment, non-blank lines of an ignore file, in file order."""
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.strip().startswith("#")]


def _assert_excluded_below_every_reinclude(path: Path, patterns: tuple[str, ...]) -> None:
    """Require `patterns` to be present and to sit below every `!` re-include.

    Docker and npm both apply the *last* matching pattern, so an exclusion placed
    above a later `!` line is silently overridden by it.
    """
    entries = _ignore_entries(path)

    missing = [p for p in patterns if p not in entries]
    assert not missing, f"{path.name} no longer excludes {missing}"

    last_reinclude = max((i for i, e in enumerate(entries) if e.startswith("!")), default=-1)
    overridden = [p for p in patterns if entries.index(p) < last_reinclude]
    assert not overridden, (
        f"{path.name} lists {overridden} above a later '!' re-include "
        f"({entries[last_reinclude]!r}), which therefore wins and ships the paths again. "
        "Move the exclusions below every '!' line."
    )


def test_dockerignore_excludes_host_build_output() -> None:
    """A local build must not change the Docker build context.

    Five of the six image assets use the repo root as their build context. Anything
    a local `pip install`, `ruff` or `mypy` run leaves under an allowlisted `src/`
    therefore shifts their asset hashes, and the next deploy rebuilds and re-pushes
    all five with no source change.
    """
    _assert_excluded_below_every_reinclude(_REPO_ROOT / ".dockerignore", _HOST_BUILD_OUTPUT)


def test_npmignore_keeps_maven_output_out_of_the_public_mirror() -> None:
    """Compiled connector jars must not ship to the public mirror.

    `connectors/` is published on purpose and is a Maven reactor, so packing a tree
    where anyone has run the build would otherwise push its `target/` jars to the
    public repository.
    """
    _assert_excluded_below_every_reinclude(_npmignore(), ("**/target/",))


def test_tests_reading_npmignore_skip_where_the_mirror_lacks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`npm pack` never ships `.npmignore`, and the GitHub PR job runs `pytest tests/unit` on what it packed.

    Both checks of the allowlist then hit a FileNotFoundError on the mirror instead of skipping there.
    """
    (tmp_path / "README.md").write_text("Run `mise install` first.\n")
    monkeypatch.setattr(sys.modules[__name__], "_REPO_ROOT", tmp_path)
    for check in (
        test_mise_config_ships_if_any_mirrored_file_says_to_run_mise_install,
        test_npmignore_keeps_maven_output_out_of_the_public_mirror,
    ):
        with pytest.raises(pytest.skip.Exception, match=".npmignore"):
            check()


@pytest.mark.parametrize(("in_source_repo", "outcome"), [(True, pytest.fail.Exception), (False, pytest.skip.Exception)])
def test_a_missing_npmignore_skips_only_on_the_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_source_repo: bool, outcome: type[BaseException]
) -> None:
    """The mirror has neither .npmignore nor ci/; a source repo that loses .npmignore must fail, not skip."""
    if in_source_repo:
        (tmp_path / "ci").mkdir()
    monkeypatch.setattr(sys.modules[__name__], "_REPO_ROOT", tmp_path)
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as raised:
        _npmignore()
    assert raised.type is outcome


# `npm pack` ships these whatever .npmignore says.
_ALWAYS_PACKED = frozenset({"package.json", "README.md", "LICENSE"})

# A JS/TS test file, by the suffixes jest and vitest pick up.
_JS_TEST_FILE = re.compile(r"\.(?:test|spec)\.(?:[cm]?[jt]sx?)$")

# A filesystem or path call. A root-entry literal on the same line is a read of that entry.
_JS_PATH_CALL = re.compile(r"\b(?:path\.(?:join|resolve)|(?:read|exists|readdir|stat)(?:File)?Sync|fromRoot)\(")


def _tracked_files() -> list[str]:
    """Every git-tracked path still on disk, repo-relative. Call after `_npmignore()`, which skips on the mirror."""
    listed = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout
    return [name for name in listed.split("\0") if name and (_REPO_ROOT / name).is_file()]


def _unmirrored_root_entries(npmignore: Path, tracked: list[str]) -> set[str]:
    """Tracked top-level files and directories that the root allowlist does not re-include."""
    reincluded = {
        entry[2:].rstrip("/")
        for entry in _ignore_entries(npmignore)
        if entry.startswith("!/") and "/" not in entry[2:].rstrip("/")
    }
    roots = {name.split("/", 1)[0] for name in tracked}
    return roots - reincluded - _ALWAYS_PACKED


def _unguarded_root_reads(source: str, unmirrored: set[str]) -> list[str]:
    """Lines of a JS/TS test that read an unmirrored root entry with no `existsSync` check on that read.

    A read counts as guarded when the same line calls `existsSync`, or when it assigns the path to a
    `const`/`let` that the file passes to `existsSync` elsewhere.
    """
    if not unmirrored:
        return []
    names = "|".join(re.escape(name) for name in sorted(unmirrored))
    literal = re.compile(r"""["'`](?:""" + names + r""")(?:/[^"'`]*)?["'`]""")
    offenders = []
    for line in source.splitlines():
        if not (literal.search(line) and _JS_PATH_CALL.search(line)) or "existsSync(" in line:
            continue
        bound = re.search(r"\b(?:const|let)\s+(\w+)\s*=", line)
        if bound and re.search(r"existsSync\(\s*" + re.escape(bound.group(1)) + r"\s*\)", source):
            continue
        offenders.append(line.strip())
    return offenders


def test_js_tests_reading_an_unmirrored_root_entry_check_that_it_is_present() -> None:
    """A mirrored jest/vitest test must not hard-require a root file or directory the mirror does not ship.

    The GitHub PR job runs `nx affected -t test` on the packed tree. A bare read there of, for example,
    `renovate.json` (Renovate runs only in the source repository) is an ENOENT that fails the whole
    project's test target, not a skip.
    """
    npmignore = _npmignore()
    tracked = _tracked_files()
    unmirrored = _unmirrored_root_entries(npmignore, tracked)
    offenders = {}
    for name in tracked:
        if not _JS_TEST_FILE.search(name) or name.split("/", 1)[0] in unmirrored:
            continue
        found = _unguarded_root_reads((_REPO_ROOT / name).read_text(), unmirrored)
        if found:
            offenders[name] = found
    assert not offenders, (
        f"{offenders} read a root entry that the public mirror does not ship. Check it with fs.existsSync "
        "and skip there, failing only where the source repository's ci/ is present."
    )


_UNMIRRORED_FOR_SELF_TEST = {"renovate.json", "ci", "demo"}


@pytest.mark.parametrize(
    "source",
    [
        'fs.readFileSync(path.join(repoRoot, "renovate.json"), "utf-8"),',
        "const text = readFileSync(path.resolve(root, 'ci/mainline.yml'));",
        "const seeds = fs.readdirSync(fromRoot(`demo`));",
    ],
)
def test_the_js_root_read_check_flags_an_unguarded_read(source: str) -> None:
    assert _unguarded_root_reads(source, _UNMIRRORED_FOR_SELF_TEST) == [source]


@pytest.mark.parametrize(
    "source",
    [
        'const renovateJson = path.join(repoRoot, "renovate.json");\nif (fs.existsSync(renovateJson)) {}',
        'const it2 = fs.existsSync(path.join(repoRoot, "ci")) ? it : it.skip;',
        'it.each(["dev", "tst", "demo", "prod"])("names %s", () => {});',
        'fs.readFileSync(path.join(repoRoot, "renovate.json.bak"), "utf-8");',
        'fs.readFileSync(path.join(repoRoot, "scripts/relock-python-requirements.sh"), "utf-8");',
    ],
)
def test_the_js_root_read_check_accepts_guarded_and_unrelated_lines(source: str) -> None:
    assert _unguarded_root_reads(source, _UNMIRRORED_FOR_SELF_TEST) == []


def test_unmirrored_root_entries_follow_the_root_allowlist(tmp_path: Path) -> None:
    npmignore = tmp_path / ".npmignore"
    npmignore.write_text("/*\n!/infra/\n!/VERSION\n**/tests/integ/\n")
    tracked = ["infra/a.ts", "VERSION", "renovate.json", "ci/mainline.yml", "package.json", "README.md"]
    assert _unmirrored_root_entries(npmignore, tracked) == {"renovate.json", "ci"}


def test_the_js_root_read_check_skips_on_a_mirror_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mirror download (ZIP or release tarball) has neither .npmignore nor .git, so the check must skip there."""
    monkeypatch.setattr(sys.modules[__name__], "_REPO_ROOT", tmp_path)
    with pytest.raises(pytest.skip.Exception, match=".npmignore"):
        test_js_tests_reading_an_unmirrored_root_entry_check_that_it_is_present()
