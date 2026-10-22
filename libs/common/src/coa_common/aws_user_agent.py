# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AWS SDK User-Agent tagging for usage attribution.

Appends the AWS Solutions token (``AWSSOLUTION/SO0376/v<version>``) to the User-Agent header of every
boto3/botocore AWS API call so AWS usage can be attributed to this solution.

The tag is injected once at the botocore *session* layer via a small,
idempotent patch of ``boto3.session.Session``. This covers every client and
resource regardless of how it is created — module-level ``boto3.client(...)``,
explicit ``boto3.Session(...)``, or assumed-role sessions — and composes with
any per-client ``botocore.config.Config`` a call site already passes (the
session's ``user_agent_extra`` is appended in addition to a client config's own
``user_agent_extra``). Centralizing here avoids having to remember to set the
header at each of the ~30 client-creation sites across the workspace.
"""

from __future__ import annotations

import boto3

from coa_common import __version__
from coa_common.aws_config import apply_default_client_config

# AWS Solutions usage-metrics token: AWSSOLUTION/<solution id>/v<version>.
SOLUTION_ID = "SO0376"
USER_AGENT_EXTRA = f"AWSSOLUTION/{SOLUTION_ID}/v{__version__}"

_installed = False


def _append_extra(botocore_session) -> None:
    """Append the tag to a botocore session's ``user_agent_extra`` once."""
    existing = getattr(botocore_session, "user_agent_extra", "") or ""
    if USER_AGENT_EXTRA not in existing.split():
        botocore_session.user_agent_extra = f"{existing} {USER_AGENT_EXTRA}".strip()


def install_user_agent() -> None:
    """Idempotently tag every boto3 client/resource's User-Agent with the solution ID.

    Safe to call multiple times; only the first call patches ``boto3.session.Session``.
    """
    global _installed
    if _installed:
        return

    original_init = boto3.session.Session.__init__

    def _patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        # ``_session`` is the underlying botocore session; its ``user_agent_extra``
        # feeds the User-Agent of every client/resource built from this session.
        _append_extra(self._session)
        # Ensure every session has socket timeouts + bounded retries by default,
        # so no client is ever created on botocore's no-timeout defaults.
        apply_default_client_config(self._session)

    _patched_init.__wrapped__ = original_init  # type: ignore[attr-defined]
    boto3.session.Session.__init__ = _patched_init  # type: ignore[method-assign]

    # A default session may already exist (if a client was built before this
    # ran); tag it so those clients get the header too.
    if boto3.DEFAULT_SESSION is not None:
        _append_extra(boto3.DEFAULT_SESSION._session)
        apply_default_client_config(boto3.DEFAULT_SESSION._session)

    _installed = True
