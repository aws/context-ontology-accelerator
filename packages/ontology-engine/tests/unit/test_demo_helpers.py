# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the pure helpers in demo.py and setup.py.

demo.py and setup.py aren't regular Python packages (they live in demo/ not
src/), so we load them via importlib.util.spec_from_file_location. Each test
mocks the minimum external surface (httpx, Prompt.ask, Bedrock) needed to
exercise a helper in isolation.

What's covered:
  demo.py
    _prompt_selection — "all"/"none"/comma list/invalid re-prompt
    _http_fetch       — TimeoutException, ConnectError, HTTPStatusError paths

  setup.py
    _build_class_text    — label+comment concatenation
    _build_property_text — label+comment+domain/range concatenation
    _find_existing_ontology — miss vs hit
    _register_ontology      — idempotent short-circuit when already present
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

pytestmark = pytest.mark.unit

_DEMO_DIR = Path(__file__).parent.parent.parent / "demo"


def _load_module(name: str, path: Path):
    """Load a Python file as a module without requiring it on sys.path.

    Both demo.py and setup.py do module-level work (read config.yaml, set
    CATALOG_URL etc.) that would normally side-effect. We provide a stub
    config before import by monkey-patching CONFIG_PATH's parent or
    preloading sys.modules as appropriate.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"couldn't load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stub_config(tmp_path):
    """Copy demo.py and setup.py into a tmp dir beside a minimal config.yaml, and yield that dir.

    Both compute CONFIG_PATH = Path(__file__).parent / "config.yaml" at module scope, so loading the copies reads the
    stub config. The tracked demo/ dir is never written, so parallel workers can't race on it.
    """
    for name in ("demo.py", "setup.py"):
        shutil.copy(_DEMO_DIR / name, tmp_path / name)
    (tmp_path / "config.yaml").write_text("""
services:
  ontology-catalog: "http://localhost:8001"
  data-catalog: "http://localhost:8003"
induction_defaults:
  ontology_uri_prefix: "https://test/"
  label: "Test"
  confidence_threshold: 0.8
  scoring_strategy: "lexical"
  strategy: "table_to_ontology"
  namespace: "default"
foundational_ontologies: []
""")
    return tmp_path


# ── demo.py helpers ─────────────────────────────────────────────


class TestPromptSelection:
    """Tests for demo._prompt_selection — the comma-separated-index parser
    with 'all'/'none' shortcuts and re-prompt on bad input.
    """

    @pytest.fixture
    def demo_module(self, stub_config):
        return _load_module("demo_under_test", stub_config / "demo.py")

    def test_all_returns_full_range(self, demo_module):
        with patch.object(demo_module, "Prompt") as mock_prompt:
            mock_prompt.ask.return_value = "all"
            result = demo_module._prompt_selection("pick", max_index=5)
        assert result == [0, 1, 2, 3, 4]

    def test_none_returns_empty_when_allowed(self, demo_module):
        with patch.object(demo_module, "Prompt") as mock_prompt:
            mock_prompt.ask.return_value = "none"
            result = demo_module._prompt_selection("pick", max_index=5, allow_none=True)
        assert result == []

    def test_numeric_list_returns_zero_indexed(self, demo_module):
        with patch.object(demo_module, "Prompt") as mock_prompt:
            mock_prompt.ask.return_value = "1,3,5"
            result = demo_module._prompt_selection("pick", max_index=5)
        assert result == [0, 2, 4]

    def test_out_of_range_ignored(self, demo_module):
        with patch.object(demo_module, "Prompt") as mock_prompt:
            mock_prompt.ask.return_value = "1,99,3"
            result = demo_module._prompt_selection("pick", max_index=5)
        assert result == [0, 2]

    def test_invalid_then_valid(self, demo_module):
        """Non-numeric input should re-prompt, not crash."""
        with patch.object(demo_module, "Prompt") as mock_prompt:
            mock_prompt.ask.side_effect = ["xyz", "1,2"]
            result = demo_module._prompt_selection("pick", max_index=5)
        assert result == [0, 1]
        assert mock_prompt.ask.call_count == 2

    def test_all_out_of_range_reprompts(self, demo_module):
        """If every number is out of range, re-prompt rather than returning []."""
        with patch.object(demo_module, "Prompt") as mock_prompt:
            mock_prompt.ask.side_effect = ["99,100", "2"]
            result = demo_module._prompt_selection("pick", max_index=5)
        assert result == [1]
        assert mock_prompt.ask.call_count == 2


class TestHttpFetch:
    """Tests for demo._http_fetch — httpx wrapper with friendly error paths."""

    @pytest.fixture
    def demo_module(self, stub_config):
        return _load_module("demo_under_test", stub_config / "demo.py")

    def test_success_returns_json(self, demo_module):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ok": True}

        with patch.object(demo_module.httpx, "request", return_value=mock_response):
            result = demo_module._http_fetch("GET", "http://fake/x")
        assert result == {"ok": True}

    def test_timeout_exits_with_message(self, demo_module):
        """Timeouts should sys.exit(1) with a friendly message, not stack-trace."""
        with (
            patch.object(demo_module.httpx, "request", side_effect=httpx.TimeoutException("slow")),
            pytest.raises(SystemExit) as excinfo,
        ):
            demo_module._http_fetch("GET", "http://fake/x")
        assert excinfo.value.code == 1

    def test_connect_error_exits(self, demo_module):
        with (
            patch.object(demo_module.httpx, "request", side_effect=httpx.ConnectError("refused")),
            pytest.raises(SystemExit),
        ):
            demo_module._http_fetch("GET", "http://fake/x")

    def test_http_status_error_exits(self, demo_module):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        err = httpx.HTTPStatusError("500", request=MagicMock(), response=mock_response)
        mock_response.raise_for_status.side_effect = err

        with (
            patch.object(demo_module.httpx, "request", return_value=mock_response),
            pytest.raises(SystemExit),
        ):
            demo_module._http_fetch("GET", "http://fake/x")


# ── setup.py helpers ────────────────────────────────────────────


class TestBuildClassText:
    @pytest.fixture
    def setup_module(self, stub_config):
        return _load_module("setup_under_test", stub_config / "setup.py")

    def test_labels_and_comments_joined(self, setup_module):
        entry = {"labels": ["Person", "Human"], "comments": ["A natural person."]}
        assert setup_module._build_class_text(entry) == "Person Human A natural person."

    def test_empty_entry(self, setup_module):
        assert setup_module._build_class_text({}) == ""

    def test_only_labels(self, setup_module):
        entry = {"labels": ["Thing"]}
        assert setup_module._build_class_text(entry) == "Thing"

    def test_only_comments(self, setup_module):
        entry = {"comments": ["A generic thing"]}
        assert setup_module._build_class_text(entry) == "A generic thing"


class TestBuildPropertyText:
    @pytest.fixture
    def setup_module(self, stub_config):
        return _load_module("setup_under_test", stub_config / "setup.py")

    def test_labels_comments_and_types(self, setup_module):
        entry = {
            "labels": ["startDate"],
            "comments": ["Event start"],
            "domain": "https://schema.org/Event",
            "range": "xsd:dateTime",
        }
        result = setup_module._build_property_text(entry)
        assert "startDate" in result
        assert "Event start" in result
        assert "domain=https://schema.org/Event" in result
        assert "range=xsd:dateTime" in result

    def test_no_domain_range_omits_context(self, setup_module):
        entry = {"labels": ["name"], "comments": ["A name"]}
        result = setup_module._build_property_text(entry)
        assert result == "name A name"
        assert "domain=" not in result

    def test_only_domain(self, setup_module):
        entry = {"labels": ["foo"], "domain": "https://schema.org/Thing"}
        result = setup_module._build_property_text(entry)
        assert "foo" in result
        assert "domain=https://schema.org/Thing" in result


class TestFindExistingOntology:
    @pytest.fixture
    def setup_module(self, stub_config):
        return _load_module("setup_under_test", stub_config / "setup.py")

    def test_hit(self, setup_module):
        """ListOntologies answers with a camelCase ``{"ontologies": [...]}`` envelope.

        The row is normalised onto the snake_case ``ontology_id`` that the POST
        response uses, because callers treat the two responses interchangeably.
        """
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "ontologies": [
                {"ontologyId": "abc", "uri": "https://schema.org/"},
                {"ontologyId": "def", "uri": "http://other/"},
            ]
        }
        with patch.object(setup_module.httpx, "get", return_value=mock_response):
            result = setup_module._find_existing_ontology("https://schema.org/")
        assert result == {
            "ontologyId": "abc",
            "uri": "https://schema.org/",
            "ontology_id": "abc",
        }

    def test_hit_on_a_legacy_bare_array(self, setup_module):
        """Deployments predating the envelope answered with a bare array."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [
            {"ontology_id": "abc", "uri": "https://schema.org/"},
        ]
        with patch.object(setup_module.httpx, "get", return_value=mock_response):
            result = setup_module._find_existing_ontology("https://schema.org/")
        assert result == {"ontology_id": "abc", "uri": "https://schema.org/"}

    def test_miss(self, setup_module):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ontologies": []}
        with patch.object(setup_module.httpx, "get", return_value=mock_response):
            result = setup_module._find_existing_ontology("https://not-registered/")
        assert result is None

    def test_miss_when_api_returns_unrelated_rows(self, setup_module):
        """The server should filter by uri, but be defensive in case it doesn't."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"ontologies": [{"ontologyId": "x", "uri": "different-uri"}]}
        with patch.object(setup_module.httpx, "get", return_value=mock_response):
            result = setup_module._find_existing_ontology("https://target/")
        assert result is None


class TestRegisterOntology:
    @pytest.fixture
    def setup_module(self, stub_config):
        return _load_module("setup_under_test", stub_config / "setup.py")

    def test_idempotent_when_already_exists(self, setup_module):
        """Re-running setup.py should not create duplicates."""
        existing = {"ontology_id": "abc", "uri": "https://schema.org/"}
        with patch.object(setup_module, "_find_existing_ontology", return_value=existing):
            result = setup_module._register_ontology({"uri": "https://schema.org/"})
        assert result == existing

    def test_creates_when_not_present(self, setup_module):
        entry = {"uri": "https://schema.org/", "title": "Schema.org", "format": "turtle"}
        created = {"id": "new-id", "uri": "https://schema.org/"}

        mock_post_response = MagicMock()
        mock_post_response.raise_for_status.return_value = None
        mock_post_response.json.return_value = created

        with (
            patch.object(setup_module, "_find_existing_ontology", return_value=None),
            patch.object(setup_module.httpx, "post", return_value=mock_post_response) as mock_post,
        ):
            result = setup_module._register_ontology(entry)

        assert result == created
        mock_post.assert_called_once()
        # Verify we sent the right body
        call_body = mock_post.call_args.kwargs["json"]
        assert call_body["uri"] == "https://schema.org/"
        assert call_body["title"] == "Schema.org"
        assert call_body["ontology_type"] == "foundational"  # default

    def test_returns_none_on_http_error(self, setup_module):
        with (
            patch.object(setup_module, "_find_existing_ontology", return_value=None),
            patch.object(setup_module.httpx, "post", side_effect=httpx.HTTPError("boom")),
        ):
            result = setup_module._register_ontology({"uri": "https://test/"})
        assert result is None


class TestServerSideFetch:
    """Tests for setup._server_side_fetch — primarily that it URL-encodes
    ontology IDs correctly (the PROV-O '#' fragment bug).
    """

    @pytest.fixture
    def setup_module(self, stub_config):
        return _load_module("setup_under_test", stub_config / "setup.py")

    def test_url_encodes_ontology_id(self, setup_module):
        """PROV-O's URI contains '#' which HTTP clients treat as a fragment
        boundary unless escaped. Previously broke /fetch with a 405.
        """
        captured_url = {"url": None}

        def fake_post(url, json=None, timeout=None):
            captured_url["url"] = url
            mock = MagicMock()
            mock.raise_for_status.return_value = None
            mock.json.return_value = {"id": "x", "uri": "x", "parse_status": "ok"}
            return mock

        with patch.object(setup_module.httpx, "post", side_effect=fake_post):
            setup_module._server_side_fetch("http://www.w3.org/ns/prov#", "http://example/src.ttl")

        url = captured_url["url"]
        assert url is not None
        # '#' must be percent-encoded as %23
        assert "%23" in url, f"expected '#' to be encoded as %23 in {url!r}"
        # And '/' must be percent-encoded as %2F (not a separator — part of the id)
        assert "%2F" in url, f"expected '/' to be encoded as %2F in {url!r}"

    def test_non_fatal_on_server_error(self, setup_module):
        """A 5xx from the server must return None, not raise — setup.py treats
        server-side fetch as best-effort and proceeds with sample_classes.
        """
        with patch.object(
            setup_module.httpx,
            "post",
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock(status_code=500)),
        ):
            result = setup_module._server_side_fetch("https://schema.org/", "http://example/")
        assert result is None


def test_demo_helpers_leave_the_original_configuration_unchanged(request: pytest.FixtureRequest) -> None:
    config = _DEMO_DIR / "config.yaml"
    before = config.read_bytes() if config.exists() else None
    request.getfixturevalue("stub_config")
    after = config.read_bytes() if config.exists() else None
    assert after == before
