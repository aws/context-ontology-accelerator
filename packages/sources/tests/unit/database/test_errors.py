# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from botocore.exceptions import ClientError
from coa_sources.database.errors import (
    PermanentScanError,
    ScanError,
    TransientScanError,
    is_permanent_scan_error,
)


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, "SomeOperation")


class TestScanErrorHierarchy:
    def test_all_subclass_runtimeerror(self):
        # Existing callers/tests catch RuntimeError; the classified errors must
        # remain compatible with that.
        assert issubclass(ScanError, RuntimeError)
        assert issubclass(TransientScanError, ScanError)
        assert issubclass(PermanentScanError, ScanError)


class TestIsPermanentScanError:
    def test_value_error_is_permanent(self):
        # Discovery raises ValueError for unsupported type / source not found /
        # too many tables — all deterministic, never worth retrying.
        assert is_permanent_scan_error(ValueError("Unsupported source type: X")) is True

    def test_permanent_client_error_codes(self):
        for code in (
            "AccessDeniedException",
            "UnauthorizedException",
            "EntityNotFoundException",
            "ResourceNotFoundException",
            "ValidationException",
            "InvalidInputException",
        ):
            assert is_permanent_scan_error(_client_error(code)) is True, code

    def test_access_denied_and_not_found_substrings(self):
        assert is_permanent_scan_error(_client_error("SomeAccessDeniedThing")) is True
        assert is_permanent_scan_error(_client_error("WidgetNotFound")) is True

    def test_transient_client_error_is_not_permanent(self):
        assert is_permanent_scan_error(_client_error("ThrottlingException")) is False
        assert is_permanent_scan_error(_client_error("InternalServerError")) is False

    def test_generic_exceptions_default_transient(self):
        assert is_permanent_scan_error(RuntimeError("boom")) is False
        assert is_permanent_scan_error(ConnectionError("network blip")) is False
