# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The verified caller, and the forwarded ID token their COA calls go out with.

The agents hold no credentials: every COA call forwards the caller's own token, so
COA applies the caller's grants. A forwarded token cannot be refreshed, so within
``_EXPIRY_MARGIN_S`` of expiry every further call raises ``CallerTokenExpiredError``;
work already started keeps running in COA.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Stop forwarding a token this close to its expiry: a request in flight must
# not reach COA with a token that expired on the way.
_EXPIRY_MARGIN_S = 60.0

EXPIRED_MESSAGE = (
    "Your COA token expired during this run. Work already started keeps running in COA; "
    "re-issue the request with a fresh token to continue."
)


class CallerTokenExpiredError(RuntimeError):
    """The caller's token expired; a new COA call needs a fresh one."""

    # Not retryable: polls treat a 4xx status as a final answer.
    status_code = 401


@dataclass(frozen=True)
class Caller:
    """The verified caller.

    Attributes:
        email: The ``email`` claim, COA's principal id.
        groups: The caller's group claim.
        id_token: The raw ID token (never logged).
        expires_at: The token's ``exp`` claim, epoch seconds.
    """

    email: str
    groups: tuple[str, ...]
    id_token: str = field(repr=False)
    expires_at: float


def token_remaining_s(caller: Caller, now: float | None = None) -> float:
    """Seconds left before ``caller``'s token reaches the expiry margin (negative once past it)."""
    return caller.expires_at - _EXPIRY_MARGIN_S - (time.time() if now is None else now)


def token_expired(caller: Caller, now: float | None = None) -> bool:
    """Return True once ``caller``'s token is within the expiry margin."""
    return token_remaining_s(caller, now) <= 0


@dataclass
class CallerTokenProvider:
    """Forwards the caller's own ID token, and refuses once it is about to expire.

    Attributes:
        caller: The verified caller.
    """

    caller: Caller
    _clock: Callable[[], float] = field(default=time.time, repr=False)

    def get_id_token(self) -> str:
        """Return the caller's token while valid.

        Raises:
            CallerTokenExpiredError: The token is within the expiry margin.
        """
        if token_expired(self.caller, self._clock()):
            raise CallerTokenExpiredError(EXPIRED_MESSAGE)
        return self.caller.id_token
