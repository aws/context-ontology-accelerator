# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_common.logging.setup_logging."""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest
from coa_common.logging import setup_logging

pytestmark = pytest.mark.unit


@pytest.fixture
def restore_root_logger():
    """Snapshot and restore the stdlib root logger around a test.

    ``setup_logging`` mutates global stdlib state, so without this a test would
    leak its handler into every subsequent test in the session.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield root
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


class TestSetupLogging:
    @patch("coa_common.logging.structlog.configure")
    def test_configures_structlog_with_default_level(self, mock_configure, restore_root_logger):
        setup_logging()
        mock_configure.assert_called_once()
        kwargs = mock_configure.call_args.kwargs
        assert kwargs["cache_logger_on_first_use"] is True
        assert kwargs["context_class"] is dict

    @patch("coa_common.logging.structlog.configure")
    def test_unknown_level_falls_back_to_info(self, mock_configure, restore_root_logger):
        # Should not raise even with a bogus level (getattr fallback to INFO)
        setup_logging(log_level="NOPE")
        mock_configure.assert_called_once()


class TestStdlibBridge:
    """The stdlib root logger must get a handler, or third-party logs are lost.

    Without one, records fall through to ``logging.lastResort`` — stderr-only,
    pinned at WARNING, ignoring logger levels — so every INFO/DEBUG line from
    graphrag-toolkit, botocore and opensearch-py is silently discarded.
    """

    def test_installs_root_handler(self, restore_root_logger):
        setup_logging("INFO")
        assert restore_root_logger.handlers, "no root handler: stdlib logs fall to lastResort and INFO is dropped"

    def test_root_level_follows_stdlib_log_level(self, restore_root_logger):
        setup_logging("INFO", stdlib_log_level="WARNING")
        assert restore_root_logger.level == logging.WARNING

    def test_root_level_defaults_to_warning_not_log_level(self, restore_root_logger):
        # Defaulting to WARNING preserves the volume lastResort already allowed,
        # so the ~35 existing callers gain structure without a log-volume jump
        # from third-party INFO (botocore in particular).
        setup_logging("DEBUG")
        assert restore_root_logger.level == logging.WARNING

    def test_unknown_stdlib_level_falls_back_to_warning(self, restore_root_logger):
        setup_logging("INFO", stdlib_log_level="BOGUS")
        assert restore_root_logger.level == logging.WARNING

    def test_replaces_existing_handlers_so_records_emit_once(self, restore_root_logger):
        # A module that already called basicConfig would otherwise double-emit.
        logging.basicConfig()
        before = len(restore_root_logger.handlers)
        assert before >= 1
        setup_logging("INFO")
        assert len(restore_root_logger.handlers) == 1

    def test_third_party_info_record_renders_as_json_with_logger_name(self, restore_root_logger, capsys):
        setup_logging("INFO", stdlib_log_level="INFO")
        logging.getLogger("graphrag_toolkit.lexical_graph.indexing.build.build_pipeline").info(
            "Running build pipeline [num_workers: 4]"
        )

        line = capsys.readouterr().err.strip()
        payload = json.loads(line)
        assert payload["event"] == "Running build pipeline [num_workers: 4]"
        assert payload["level"] == "info"
        assert payload["logger"] == "graphrag_toolkit.lexical_graph.indexing.build.build_pipeline"
        assert "timestamp" in payload

    def test_third_party_debug_suppressed_at_info(self, restore_root_logger, capsys):
        setup_logging("INFO", stdlib_log_level="INFO")
        logging.getLogger("graphrag_toolkit.x").debug("batch retry ladder detail")
        assert capsys.readouterr().err == ""

    def test_third_party_info_suppressed_by_default(self, restore_root_logger, capsys):
        # Callers that don't opt in keep today's volume: WARNING and above only.
        setup_logging("INFO")
        logging.getLogger("botocore.endpoint").info("Making request to ...")
        assert capsys.readouterr().err == ""

    def test_third_party_debug_emitted_when_dependency_level_is_debug(self, restore_root_logger, capsys):
        setup_logging("INFO", stdlib_log_level="DEBUG")
        logging.getLogger("graphrag_toolkit.x").debug("batch retry ladder detail")
        payload = json.loads(capsys.readouterr().err.strip())
        assert payload["event"] == "batch retry ladder detail"
        assert payload["level"] == "debug"


class TestExtraProcessors:
    """``extra_processors`` run last before rendering, on structlog and stdlib lines alike."""

    @staticmethod
    def _tag(_logger, _method, event_dict):
        event_dict["tagged"] = True
        event_dict.pop("secret", None)
        return event_dict

    def test_extra_processor_runs_on_stdlib_records(self, restore_root_logger, capsys):
        setup_logging("INFO", extra_processors=[self._tag])
        logging.getLogger("botocore.endpoint").warning("slow")
        payload = json.loads(capsys.readouterr().err.strip())
        assert payload["tagged"] is True
        assert payload["event"] == "slow"

    @patch("coa_common.logging.structlog.configure")
    def test_extra_processor_runs_before_the_structlog_renderer(self, mock_configure, restore_root_logger):
        setup_logging("INFO", extra_processors=[self._tag])
        processors = mock_configure.call_args.kwargs["processors"]
        assert processors[-2] is self._tag

    @patch("coa_common.logging.structlog.configure")
    def test_no_extra_processors_by_default(self, mock_configure, restore_root_logger):
        setup_logging("INFO")
        processors = mock_configure.call_args.kwargs["processors"]
        assert self._tag not in processors
