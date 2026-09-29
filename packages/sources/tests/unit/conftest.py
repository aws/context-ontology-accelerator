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
from unittest.mock import patch

import pytest

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

# ``sources_handler`` captures ``SOURCES_TABLE`` at IMPORT time (module-level
# ``_SOURCES_TABLE``), so whichever test module imports it FIRST decides whether
# the DAO can be built at all — every later `_get_dao()` raises "SOURCES_TABLE env
# var not set" if that first import saw no value. test_sources_handler.py used to
# be that module and set the var itself, which held only because it happened to
# sort before the other importers. Any new module importing the handler earlier,
# directly or transitively, silently broke ~14 of its tests. Setting it here —
# before pytest imports any test module — removes the ordering dependence, the
# same reasoning as the AWS_REGION block above. The value matches the table
# test_sources_handler.py creates in moto.
os.environ.setdefault("SOURCES_TABLE", "test-sources")

# Imported after the env block above: the module reads AWS_REGION at import time.
from coa_sources.database import glue_ownership  # noqa: E402

_STUB_ACCOUNT = "000000000000"


@pytest.fixture(autouse=True)
def stub_glue_ownership(request):
    """Treat every Glue database as shared with all namespaces, unless opted out.

    Creating or scanning a Glue source now requires the target database to be
    registered to the caller's namespace — a live ``glue:GetTags`` call and an STS
    identity lookup (see ``coa_sources.database.glue_ownership``). Neither is what
    the tests around them are about, and unstubbed both reach for the network and
    then fail closed, turning every Glue fixture into a 403.

    The ownership check itself is covered directly in
    ``tests/unit/database/test_glue_ownership.py`` and the route/pipeline tests that
    carry the ``real_glue_ownership`` marker, which this fixture steps aside for.
    Reach for that marker in any new test where the ownership decision is the
    subject rather than a precondition.
    """
    if request.node.get_closest_marker("real_glue_ownership"):
        yield
        return
    shared = glue_ownership._TagLookup(frozenset({glue_ownership.SHARED_WITH_ALL}), missing=False)
    with (
        patch.object(glue_ownership, "_tagged_namespaces", return_value=shared),
        patch.object(glue_ownership, "_deployment_account", return_value=_STUB_ACCOUNT),
    ):
        yield
