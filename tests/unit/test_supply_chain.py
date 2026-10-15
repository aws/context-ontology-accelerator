# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for third-party NOTICE generation (ORR PAK-A-3)."""

from __future__ import annotations

import pytest

from scripts.supply_chain import licenses, notice
from scripts.supply_chain.licenses import PackageLicense

pytestmark = pytest.mark.unit


# --- normalize_license -------------------------------------------------------


@pytest.mark.parametrize(
    "raw,classifiers,expected",
    [
        ("Apache-2.0", [], "Apache-2.0"),
        ("MIT", [], "MIT"),
        ("", ["License :: OSI Approved :: MIT License"], "MIT"),
        ("", ["License :: OSI Approved :: Apache Software License"], "Apache-2.0"),
        ("", ["License :: OSI Approved :: BSD License"], "BSD-3-Clause"),
        ("BSD-3-Clause", [], "BSD-3-Clause"),
        ("The Unlicense (Unlicense)", [], "Unlicense"),
        ("", ["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"], "GPL-3.0"),
        ("", ["License :: OSI Approved :: GNU Affero General Public License v3"], "AGPL-3.0"),
        ("", [], licenses.UNKNOWN),
        (None, [], licenses.UNKNOWN),
        ("Totally Custom Proprietary Thing", [], licenses.UNKNOWN),
    ],
)
def test_normalize_license(raw, classifiers, expected):
    assert licenses.normalize_license(raw, classifiers) == expected


def test_normalize_license_prefers_classifier_over_freetext_noise():
    # Multi-line raw (pasted license text) is noise; classifier wins.
    raw = "MIT License\n\nPermission is hereby granted, free of charge..."
    assert licenses.normalize_license(raw, ["License :: OSI Approved :: MIT License"]) == "MIT"


def test_raw_license_multiline_is_dropped():
    # A long single-line license still alias-matches "mit".
    long_text = "MIT " + "x" * 80
    assert licenses.normalize_license(long_text, []) == "MIT"


# --- inventory (iter_installed / first-party / overrides) --------------------


def _pkg(name, norm, ver="1.0.0", raw=""):
    return PackageLicense(name=name, version=ver, raw_license=raw, normalized=norm)


def test_is_first_party():
    assert licenses.is_first_party("coa-common")
    assert licenses.is_first_party("coa_serve")
    assert not licenses.is_first_party("boto3")


def test_iter_installed_excludes_first_party_and_applies_overrides(monkeypatch):
    class _FakeMeta(dict):
        def get_all(self, key):
            return {"Classifier": self.get("_classifiers", [])}.get(key, [])

    class _FakeDist:
        def __init__(self, name, version, raw="", classifiers=()):
            self.metadata = _FakeMeta(
                {"Name": name, "Version": version, "License": raw, "_classifiers": list(classifiers)}
            )

    fake = [
        _FakeDist("boto3", "1.43.0", "Apache-2.0"),
        _FakeDist("coa-common", "0.0.1", ""),  # first-party → excluded
        _FakeDist("cedarpy", "4.8.3", ""),  # override → Apache-2.0
        _FakeDist("owlrl", "7.1.4", "W3C software and Document Notice License"),  # override → W3C
    ]
    monkeypatch.setattr(licenses.im, "distributions", lambda: iter(fake))

    pkgs = {p.name: p for p in licenses.iter_installed()}
    assert "coa-common" not in pkgs
    assert pkgs["boto3"].normalized == "Apache-2.0"
    assert pkgs["cedarpy"].normalized == "Apache-2.0"
    assert pkgs["owlrl"].normalized == "W3C"


@pytest.mark.parametrize("classifiers", [[], ["License :: OSI Approved :: Apache Software License"]])
@pytest.mark.parametrize("name", ["pypdfium2", "cedarpy"])
def test_inventory_and_notice_preserve_compound_license_declaration(monkeypatch, classifiers, name):
    class _FakeMeta(dict):
        def get_all(self, key):
            return classifiers if key == "Classifier" else []

    class _FakeDist:
        metadata = _FakeMeta(
            {
                "Name": name,
                "Version": "5.13.0",
                "License": "BSD-3-Clause, Apache-2.0, dependency licenses",
            }
        )

    monkeypatch.setattr(licenses.im, "distributions", lambda: iter([_FakeDist()]))

    packages = licenses.iter_installed()
    assert packages[0].normalized == licenses.UNKNOWN
    assert packages[0].raw_license == "BSD-3-Clause, Apache-2.0, dependency licenses"
    assert f"{name} (5.13.0) — BSD-3-Clause, Apache-2.0, dependency licenses" in notice.render_notice(packages)


# --- notice ------------------------------------------------------------------


def test_render_notice_lists_all_packages_with_attribution():
    body = notice.render_notice([_pkg("boto3", "Apache-2.0", "1.43.0"), _pkg("attrs", "MIT", "23.0.0")])
    assert "boto3 (1.43.0) — Apache-2.0" in body
    assert "attrs (23.0.0) — MIT" in body
    # attrs sorts before boto3 (case-insensitive)
    assert body.index("attrs") < body.index("boto3")


def test_render_notice_unknown_falls_back_to_raw():
    body = notice.render_notice([_pkg("weird", licenses.UNKNOWN, "1.0", raw="Custom-1.0")])
    assert "weird (1.0) — Custom-1.0" in body


def test_render_notice_unknown_without_raw_says_see_project():
    body = notice.render_notice([_pkg("mystery", licenses.UNKNOWN, "1.0", raw="")])
    assert "mystery (1.0) — see project" in body


def test_render_notice_unpinned_version():
    body = notice.render_notice([_pkg("ghost", "MIT", ver="")])
    assert "ghost (unpinned) — MIT" in body
