# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Snowflake account-availability classifier shared between integ and unit suites.

Extracted here (outside ``tests/integ/``) so the unit suite can import it in
environments where integ tests are stripped (e.g. the public GitHub mirror).
"""

from __future__ import annotations

import pytest

# Deliberately narrow: only account-level death, where NOTHING about our
# request could have caused it. Notably absent is "does not exist or not
# authorized" — that is what a wrong warehouse/database value looks like too, so
# tolerating it would mask exactly the defect these tests exist to catch.
_ACCOUNT_UNAVAILABLE_SIGNATURES = (
    "trial has ended",
    "trial has expired",
    "trial account",
    "account is suspended",
    "account has been suspended",
    "account is locked",
    "account has been locked",
    "account is deactivated",
    "incorrect username or password",
    "user is disabled",
    "user access is disabled",
)


def _account_unavailable_reason(message: str) -> str | None:
    """Return the matched signature when a failure is the Snowflake ACCOUNT dying.

    ``message`` is the failed scan job's ``errorMessage`` (see
    ``detail_helpers.source_scan_error``).
    """
    lowered = str(message or "").lower()
    return next((sig for sig in _ACCOUNT_UNAVAILABLE_SIGNATURES if sig in lowered), None)


def _skip_if_account_unavailable(message: str) -> None:
    """Skip (not fail) when the Snowflake account itself is no longer usable.

    An expired trial is an environment problem, not a regression: no change to
    this repo can make the scan pass, so failing here would be noise on every
    pipeline run until someone re-provisions the account.
    """
    reason = _account_unavailable_reason(message)
    if reason:
        pytest.skip(
            f"Snowflake account unavailable ({reason!r}) — the integ trial has most likely lapsed. "
            f"Re-point SNOWFLAKE_HOST/SNOWFLAKE_SECRET_ARN at a live account. "
            f"Full message: {message}"
        )
