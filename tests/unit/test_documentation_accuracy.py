# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Test that documentation examples match actual codebase patterns."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path


def test_package_guide_references_real_packages() -> None:
    """Verify that package-guide.md references real packages in the monorepo."""
    package_guide = Path(__file__).parent.parent.parent / "external-docs" / "content" / "package-guide.md"
    packages_dir = Path(__file__).parent.parent.parent / "packages"
    libs_dir = Path(__file__).parent.parent.parent / "libs"

    content = package_guide.read_text()

    parts = content.split("## Packages")
    assert len(parts) >= 2, "package-guide.md must contain a '## Packages' section"
    section_parts = parts[1].split("## Shared Libraries")
    packages_section = section_parts[0]

    package_table_pattern = r"\| `([^`]+)`\s+\|"
    documented_packages = re.findall(package_table_pattern, packages_section)

    existing_packages = {p.name for p in packages_dir.iterdir() if p.is_dir()}

    for pkg in documented_packages:
        assert pkg in existing_packages, f"Package '{pkg}' is documented but doesn't exist in packages/"

    assert "libs/common" in content
    assert (libs_dir / "common").exists()


def test_package_guide_pyproject_example_matches_pattern() -> None:
    """Verify pyproject.toml example matches real package patterns."""
    package_guide = Path(__file__).parent.parent.parent / "external-docs" / "content" / "package-guide.md"
    sources_pyproject = Path(__file__).parent.parent.parent / "packages" / "sources" / "pyproject.toml"

    guide_content = package_guide.read_text()
    real_pyproject = sources_pyproject.read_text()

    assert "[project]" in real_pyproject
    assert "requires-python" in real_pyproject
    assert "[build-system]" in real_pyproject
    assert "[tool.pytest.ini_options]" in real_pyproject
    assert "[tool.uv.sources]" in real_pyproject

    assert "[project]" in guide_content
    assert "[build-system]" in guide_content
    assert "[tool.pytest.ini_options]" in guide_content


def test_package_guide_project_json_example_matches_pattern() -> None:
    """Verify project.json example matches real Nx configuration."""
    package_guide = Path(__file__).parent.parent.parent / "external-docs" / "content" / "package-guide.md"
    sources_project = Path(__file__).parent.parent.parent / "packages" / "sources" / "project.json"

    guide_content = package_guide.read_text()
    real_project_json = sources_project.read_text()

    assert '"lint"' in real_project_json
    assert '"test"' in real_project_json
    assert '"format"' in real_project_json

    assert '"lint"' in guide_content
    assert '"test"' in guide_content
    assert '"format"' in guide_content


def test_package_guide_references_common_lib() -> None:
    """Verify package-guide.md correctly references libs/common utilities."""
    package_guide = Path(__file__).parent.parent.parent / "external-docs" / "content" / "package-guide.md"
    common_src = Path(__file__).parent.parent.parent / "libs" / "common" / "src" / "coa_common"

    guide_content = package_guide.read_text()

    assert "coa_common.config" in guide_content
    assert "coa_common.logging" in guide_content
    assert "coa_common.exceptions" in guide_content
    assert "coa_common.s3" in guide_content
    assert "coa_common.dao" in guide_content
    assert "coa_common.constants" in guide_content

    # Verify the referenced modules actually exist (some are packages, some are .py files)
    for module in ("config", "logging", "exceptions", "s3", "dao", "constants"):
        is_file = (common_src / f"{module}.py").exists()
        is_pkg = (common_src / module / "__init__.py").exists()
        assert is_file or is_pkg, f"libs/common module '{module}' should exist"


def test_workspace_members_registration_matches_reality() -> None:
    """Verify root pyproject.toml workspace matches documented pattern."""
    root_pyproject = Path(__file__).parent.parent.parent / "pyproject.toml"

    content = root_pyproject.read_text()

    assert "[tool.uv.workspace]" in content
    assert "members = [" in content

    assert '"packages/sources"' in content
    assert '"libs/common"' in content


def test_getting_started_package_guide_link_is_valid() -> None:
    """Verify getting-started.md links to package-guide.md correctly."""
    getting_started = Path(__file__).parent.parent.parent / "external-docs" / "content" / "getting-started.md"
    package_guide = Path(__file__).parent.parent.parent / "external-docs" / "content" / "package-guide.md"

    getting_started_content = getting_started.read_text()

    assert "package-guide.md" in getting_started_content
    assert package_guide.exists(), "package-guide.md should exist"

    package_guide_content = package_guide.read_text()
    assert "Adding a New Package" in package_guide_content
    assert "Implementing a Package" in package_guide_content


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CROSS_ARCH_HEADING = "### Cross-architecture container builds"


def _locally_built_platforms() -> dict[str, list[str]]:
    """Map each `Platform.LINUX_*` pinned by a local image build to its Dockerfiles.

    Covers both ways the stacks build an image: `ContainerImage.fromAsset(...)` and
    `new DockerImageAsset(...)` (Serve and MCP use the latter).
    """
    platforms: dict[str, list[str]] = {}
    build = r"(?:fromAsset\(|new DockerImageAsset\()(?:(?!\}\);).)*?Platform\.LINUX_(AMD64|ARM64)"
    for ts in sorted((_REPO_ROOT / "infra" / "lib" / "stacks").rglob("*.ts")):
        text = ts.read_text()
        for match in re.finditer(build, text, re.DOTALL):
            dockerfile = re.search(r'file:\s*"([^"]+)"', match.group(0))
            platforms.setdefault(match.group(1).lower(), []).append(dockerfile.group(1) if dockerfile else ts.name)
    return platforms


def _cross_arch_section() -> str:
    text = (_REPO_ROOT / "external-docs" / "content" / "deploying.md").read_text()
    match = re.search(re.escape(_CROSS_ARCH_HEADING) + r"\n(.*?)(?=\n#{2,3} )", text, re.DOTALL)
    assert match, f"deploying.md must keep a '{_CROSS_ARCH_HEADING}' section"
    return match.group(1)


def test_cross_arch_scan_sees_every_platform_pin_in_the_stacks() -> None:
    """Every `Platform.LINUX_*` pin must belong to a build the scan recognizes.

    Otherwise a new way of building an image would drop out of the doc check above.
    """
    stacks = sorted((_REPO_ROOT / "infra" / "lib" / "stacks").rglob("*.ts"))
    pins = sum(len(re.findall(r"Platform\.LINUX_(?:AMD64|ARM64)", ts.read_text())) for ts in stacks)
    found = sum(len(dockerfiles) for dockerfiles in _locally_built_platforms().values())
    assert found == pins, f"scan found {found} of {pins} platform pins: {_locally_built_platforms()}"


def test_deploying_cross_arch_section_covers_every_locally_built_platform() -> None:
    """Each image architecture the stacks build locally must be named, with its binfmt install."""
    platforms = _locally_built_platforms()
    assert set(platforms) == {"amd64", "arm64"}, f"precondition: stacks build both architectures, got {platforms}"
    section = _cross_arch_section()
    for arch, dockerfiles in platforms.items():
        assert f"linux/{arch}" in section, f"section never names linux/{arch}, built from {dockerfiles}"
        assert f"tonistiigi/binfmt --install {arch}" in section, f"section never shows how to emulate {arch}"


def test_deploying_cross_arch_section_does_not_say_a_native_host_needs_no_emulation() -> None:
    """With images for both architectures, no build host can skip emulation."""
    assert "needs no emulation" not in _cross_arch_section()


def test_preflight_points_at_deploying_sections_that_exist() -> None:
    """A preflight error that sends readers to a deploying.md section must name a real heading."""
    script = (_REPO_ROOT / "scripts" / "preflight-deploy.sh").read_text()
    pointers = re.findall(r"see external-docs/content/deploying\.md, '([^']+)'", script)
    assert pointers, "precondition: preflight points readers at a deploying.md section"
    deploying = (_REPO_ROOT / "external-docs" / "content" / "deploying.md").read_text()
    headings = set(re.findall(r"^#{2,3} (.+)$", deploying, re.MULTILINE))
    for pointer in pointers:
        assert pointer in headings, f"preflight points at '{pointer}', which deploying.md has no heading for"


def _markdown_section(doc: Path, heading: str) -> str:
    """Return the body of the section titled ``heading``, up to the next heading at the same or a higher level."""
    lines = doc.read_text().splitlines()
    in_fence = False
    start: int | None = None
    level = 0
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        heading_match = None if in_fence else re.match(r"^(#+) (.+?)\s*$", line)
        if not heading_match:
            continue
        if start is None and heading_match.group(2) == heading:
            start, level = i + 1, len(heading_match.group(1))
        elif start is not None and len(heading_match.group(1)) <= level:
            return "\n".join(lines[start:i])
    assert start is not None, f"{doc.name} has no '{heading}' heading"
    return "\n".join(lines[start:])


def _ts_constant(ts_file: Path, name: str) -> str:
    """Return the literal of ``export const <name> = <literal>;`` (quotes stripped)."""
    match = re.search(rf'^export const {name} = "?([^";]+)"?;', ts_file.read_text(), re.MULTILINE)
    assert match, f"{ts_file.name} defines no constant {name}"
    return match.group(1)


def test_first_user_steps_add_the_user_to_the_admin_group() -> None:
    """A user created by the first-user steps can only reach the Permissions page once it is in the Admin group."""
    admin_group = _ts_constant(_REPO_ROOT / "infra" / "lib" / "constants.ts", "DEFAULT_ADMIN_GROUP")
    section = _markdown_section(_REPO_ROOT / "external-docs" / "content" / "deploying.md", "1. Create Your First User")
    assert "aws cognito-idp admin-add-user-to-group" in section
    assert f"--group-name {admin_group}" in section
    assert "authentication-setup.md" in section


def test_setup_script_only_suggests_make_targets_that_exist() -> None:
    """`make setup` must not end by pointing at a target the Makefile does not define."""
    makefile = (_REPO_ROOT / "Makefile").read_text()
    targets = set(re.findall(r"^([A-Za-z0-9_-]+):", makefile, re.MULTILINE))
    hints = re.findall(r"make ([A-Za-z0-9_-]+)", (_REPO_ROOT / "scripts" / "setup-dev.sh").read_text())
    assert hints, "precondition: setup-dev.sh suggests a next make target"
    for hint in hints:
        assert hint in targets, f"setup-dev.sh suggests 'make {hint}', which the Makefile does not define"


def test_deployed_stack_table_names_stacks_by_prefix_and_env() -> None:
    """Stack names follow `<prefix>-<env>-<name>`; a deployer with their own SCL_PREFIX has no `coa-dev-*` stacks."""
    app_lines = (_REPO_ROOT / "infra" / "bin" / "app.ts").read_text().splitlines()
    app = "\n".join(line for line in app_lines if not line.lstrip().startswith("//"))
    assert "const stackPrefix = `${prefix}-${envName}`;" in app, "precondition: stack names are <prefix>-<env>-<name>"
    stack_suffixes = set(re.findall(r"`\$\{stackPrefix\}-([a-z0-9-]+)`", app))
    section = _markdown_section(_REPO_ROOT / "external-docs" / "content" / "deploying.md", "Stacks Deployed")
    rows = re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE)
    assert rows, "precondition: the section has a stack table"
    for row in rows:
        assert row.startswith("<prefix>-<env>-"), f"stack '{row}' is not written as <prefix>-<env>-<name>"
        suffix = row.removeprefix("<prefix>-<env>-")
        assert suffix in stack_suffixes, f"stack '{row}' is not created by infra/bin/app.ts"


def _python_constant(py_file: Path, name: str) -> object:
    """Evaluate the literal assigned to module-level ``name`` (a frozenset call counts as its set literal)."""
    for node in ast.parse(py_file.read_text()).body:
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(isinstance(t, ast.Name) and t.id == name for t in targets) and node.value is not None:
                value = node.value
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset":
                    value = value.args[0]
                return ast.literal_eval(value)
    raise AssertionError(f"{py_file.name} assigns no {name}")


def test_document_upload_docs_match_the_supported_formats_and_size_limit() -> None:
    """An upload the docs allow (e.g. HTML) must not be skipped by preprocessing as an unsupported format."""
    constants = _REPO_ROOT / "libs" / "common" / "src" / "coa_common" / "constants.py"
    extensions = _python_constant(constants, "SUPPORTED_EXTENSIONS")
    max_mb = _python_constant(constants, "DEFAULT_MAX_FILE_SIZE_MB")
    deployed_mb = int(_ts_constant(_REPO_ROOT / "infra" / "lib" / "constants.ts", "DEFAULT_MAX_FILE_SIZE_MB"))
    assert deployed_mb == max_mb, "precondition: infra and libs/common agree on the default file size limit"
    handler = _REPO_ROOT / "packages" / "sources" / "src" / "coa_sources" / "api" / "sources_handler.py"
    max_files = _python_constant(handler, "_MAX_UPLOAD_FILES")
    section = _markdown_section(_REPO_ROOT / "external-docs" / "content" / "sources.md", "Local Upload")
    step = re.search(r"choose up to \d+ files in (.*?) format", " ".join(section.split()))
    assert step, "precondition: the upload step names the accepted formats"
    named = set(re.findall(r"`(\.[a-z]+)`", step.group(1)))
    assert named == set(extensions), (
        f"the upload step names {sorted(named)}, but preprocessing accepts {sorted(extensions)}"
    )
    assert not re.search(r"html", section, re.IGNORECASE), "HTML is not a supported document format"
    assert f"{max_mb} MB" in section, f"Local Upload does not state the {max_mb} MB default limit"
    assert f"up to {max_files} files" in section, f"Local Upload does not state the {max_files}-file cap"
    api_section = _markdown_section(_REPO_ROOT / "external-docs" / "content" / "sources.md", "Via the API")
    assert "`filename`" in api_section and "`fileName`" not in api_section, "the upload-urls field is `filename`"


def test_tier1_docs_describe_name_matching_not_semantic_search() -> None:
    """Tier 1 matches names and synonyms as strings; docs saying it embeds the question oversell it."""
    resolver = _REPO_ROOT / "packages" / "context-manager" / "src" / "coa_serve" / "tier1" / "metric_resolver.py"
    threshold = _python_constant(resolver, "_FUZZY_MATCH_THRESHOLD")
    sections = {
        "metrics.md": _markdown_section(
            _REPO_ROOT / "external-docs" / "content" / "metrics.md", "How Metrics Are Used in Queries"
        ),
        "serve.md": _markdown_section(
            _REPO_ROOT / "external-docs" / "content" / "serve.md", "Tier 1 — Metric Resolution"
        ),
    }
    for doc, section in sections.items():
        assert "semantic similarity" not in section, f"{doc} says Tier 1 matches by semantic similarity"
        assert "Embeds the query" not in section, f"{doc} says Tier 1 embeds the query"
        assert "fuzzy" in section, f"{doc} does not mention the fuzzy fallback"
        stated = {float(x) for x in re.findall(r"\b\d\.\d+\b", section)}
        assert threshold in stated, f"{doc} does not state the fuzzy threshold {threshold}"


def test_standard_grounding_docs_describe_the_embedding_score_fallback() -> None:
    """STANDARD grounds on embedding score when no unique exact name matches, not on exact names only."""
    grounding = (
        _REPO_ROOT / "packages" / "ontology-engine" / "src" / "coa_ontology" / "inducer" / "services" / "grounding.py"
    )
    assert "STANDARD" in _python_constant(grounding, "GROUNDING_MODES"), "precondition: STANDARD is a grounding mode"
    source = grounding.read_text()
    ladder = source[
        source.index("# STANDARD mode: use embedding scores") : source.index("if recall_spread is not None")
    ]
    bounds = re.findall(r"score >= (\d\.\d+)", ladder)
    default_threshold = re.search(r"confidence_threshold: float = (\d\.\d+)", source)
    assert len(bounds) == 2 and default_threshold, "precondition: STANDARD ladder is exact/ambiguous + threshold"
    section = _markdown_section(_REPO_ROOT / "external-docs" / "content" / "ontologies.md", "Grounding")
    rows = [line for line in section.splitlines() if line.startswith("| `STANDARD` |")]
    assert len(rows) == 1, "precondition: the grounding-mode table has one STANDARD row"
    assert "exact-name matches only" not in rows[0]
    assert "embedding" in rows[0]
    tiers = {tier: float(value) for tier, value in re.findall(r"`(exact|ambiguous)` at (\d\.\d+)", rows[0])}
    assert tiers == {"exact": float(bounds[0]), "ambiguous": float(bounds[1])}, f"STANDARD row tiers {tiers} != code"
    threshold_stated = re.search(r"`confidenceThreshold` \(default (\d\.\d+)\)", rows[0])
    assert threshold_stated and float(threshold_stated.group(1)) == float(default_threshold.group(1))
    assert "only trust exact-name alignment" not in section


def _smithy_operations() -> set[str]:
    """Names of every operation declared in the Smithy models (generated build output excluded)."""
    operations: set[str] = set()
    for smithy in (_REPO_ROOT / "models" / "src").rglob("*.smithy"):
        operations.update(re.findall(r"^operation (\w+)", smithy.read_text(), re.MULTILINE))
    return operations


def test_docs_home_does_not_claim_accepted_ontologies_can_be_edited() -> None:
    """The API edits proposals before accept; it has no operation that edits an accepted ontology."""
    operations = _smithy_operations()
    assert "UpdateProposal" in operations, "precondition: proposals are editable"
    assert not {op for op in operations if re.fullmatch(r"(Update|Put|Patch|Edit|Modify|Replace|Set)Ontology\w*", op)}
    hero = " ".join((_REPO_ROOT / "external-docs" / "src" / "components" / "Hero.tsx").read_text().split())
    assert "edit their ontologies" not in hero
    assert "edit each proposed ontology before they accept it" in hero
    lifecycle = _markdown_section(_REPO_ROOT / "external-docs" / "content" / "ontologies.md", "Ontology Lifecycle")
    assert "| **Update** |" not in lifecycle, "the lifecycle table lists an Update action the API does not have"


def test_ontology_api_docs_name_only_operations_that_exist() -> None:
    """A bold operation name in the ontology API sections must be a real Smithy operation (e.g. no CreateOntology)."""
    operations = _smithy_operations()
    ontologies = _REPO_ROOT / "external-docs" / "content" / "ontologies.md"
    for heading in ("Via the API", "Listing proposals"):
        named = re.findall(r"\*\*([A-Z][A-Za-z]+)\*\*", _markdown_section(ontologies, heading))
        assert named, f"precondition: '{heading}' names at least one operation"
        for name in named:
            assert name in operations, f"'{heading}' names {name}, which is not a Smithy operation"


def test_list_proposals_docs_cover_paging_and_every_rejected_filter() -> None:
    """ListProposals pages with nextToken and rejects unknown filter values with 400; the docs must say both."""
    proposals = (_REPO_ROOT / "packages" / "ontology-engine" / "src" / "coa_ontology" / "proposals.py").read_text()
    checked = re.findall(r'require_enum_value\("(\w+)"', proposals)
    assert checked, "precondition: ListProposals validates its enum filters"
    assert len(checked) == proposals.count("require_enum_value("), "precondition: every enum check names its filter"
    section = _markdown_section(_REPO_ROOT / "external-docs" / "content" / "ontologies.md", "Listing proposals")
    assert '"nextToken"' in section and "until a" in section, "the section must say to page until nextToken is absent"
    prose = " ".join(section.split())
    assert "A client that reads only the first page can miss an open proposal" in prose
    assert "a proposal from the same kind of source is still open fails with `409`" in prose
    assert not re.search(r"\b(short|empty)\b[^.]*\bpage\b|\bpage\b[^.]*\b(short|empty)\b", prose), (
        "a ListProposals page is short only when it is the last one"
    )
    assert "read budget" not in section and "fewer than `maxResults`" not in section, (
        "ListProposals drains every match before slicing a page, so a page is never short while nextToken is present"
    )
    assert "`400`" in section
    for param in checked:
        assert f"`{param}`" in section, f"the section does not name the rejected filter {param}"


def test_list_proposals_example_uses_the_keys_the_server_sends() -> None:
    """The example proposal must use the stored row's keys (e.g. proposal_id), which the route returns unchanged."""
    section = _markdown_section(_REPO_ROOT / "external-docs" / "content" / "ontologies.md", "Listing proposals")
    example = json.loads(re.search(r"```json\n(.*?)\n```", section, re.DOTALL).group(1))
    keys = {key for key in example["proposals"][0] if key != "…"}
    assert keys, "precondition: the example shows at least one proposal key"
    store = (_REPO_ROOT / "packages" / "ontology-engine" / "src" / "coa_ontology" / "dynamo_store.py").read_text()
    for key in keys:
        assert f'"{key}": ' in store, f"the example uses {key}, which the proposal row does not store"
