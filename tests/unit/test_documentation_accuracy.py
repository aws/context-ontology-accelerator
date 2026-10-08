# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Test that documentation examples match actual codebase patterns."""

from __future__ import annotations

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
