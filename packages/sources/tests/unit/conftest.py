# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sources unit test configuration.

``ALLOWED_ORIGIN`` is required by coa_common.response (lazy check at
api_response() call time). Set before any handler imports so api_response() can
return error responses; otherwise every handler test raises
RuntimeError("ALLOWED_ORIGIN environment variable must be set"). Matches the
pattern used by control-plane and metric-service unit conftests.

``AWS_REGION`` is pinned — not ``setdefault``-ed — because the handlers resolve
their region via ``coa_common.resolve_region()``, which reads ``AWS_REGION``
FIRST and only then falls back to ``AWS_DEFAULT_REGION``. The moto fixtures
create their tables in us-east-1 and several test modules set only
``AWS_DEFAULT_REGION``, so a developer (or CI runner) with ``AWS_REGION`` exported
to anything else had the handler build its DAO against that region while the
table lived in us-east-1 — every query failed with ResourceNotFoundException and
the handler returned 500. That made ~10 tests fail depending on nothing but the
shell they ran in.
"""

import os

os.environ.setdefault("ALLOWED_ORIGIN", "https://test.example.com")

# Overwrite, don't setdefault: an inherited value is exactly the problem.
os.environ["AWS_REGION"] = "us-east-1"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
# moto rejects requests with no credentials; keep them obviously fake so a
# misconfigured test can never reach real AWS.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
