# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""@sensitive members must not become pydantic ``SecretStr`` in the server models.

Smithy renders an @sensitive string as OpenAPI ``format: password``, and
openapi-generator maps that to ``SecretStr``. A handler that serializes a response
through such a model returns ``"**********"`` instead of the value (a presigned URL,
a principal id, a query result). ``scripts/smithy-generate.sh`` strips the format
from the copy of each spec it generates the Python models from; these tests pin
that, while the published specs keep the marker for client-side log redaction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_GENERATED = _ROOT / "smithy-generated"
_SERVER_MODEL_DIRS = ("control-plane-python-server", "data-layer-python-server")
_PUBLISHED_SPECS = ("ControlPlaneService.openapi.json", "DataLayerService.openapi.json")


def _require_generated() -> None:
    if not (_GENERATED / _SERVER_MODEL_DIRS[0]).is_dir():
        pytest.skip("smithy-generated/ not present; run ./scripts/smithy-generate.sh")


@pytest.mark.parametrize("model_dir", _SERVER_MODEL_DIRS)
def test_server_models_never_use_secret_str(model_dir: str) -> None:
    _require_generated()
    offenders = sorted(
        str(path.relative_to(_GENERATED))
        for path in (_GENERATED / model_dir).rglob("*.py")
        if "SecretStr" in path.read_text()
    )
    assert not offenders, f"SecretStr would mask @sensitive values in responses: {offenders}"


@pytest.mark.parametrize("spec_name", _PUBLISHED_SPECS)
def test_published_specs_keep_the_sensitive_marker(spec_name: str) -> None:
    _require_generated()
    spec = (_GENERATED / "openapi" / "openapi" / spec_name).read_text()
    assert '"format": "password"' in spec


@pytest.mark.parametrize("spec_name", _PUBLISHED_SPECS)
def test_python_codegen_specs_drop_the_password_format(spec_name: str) -> None:
    _require_generated()
    spec = json.loads((_GENERATED / "openapi" / "python-codegen" / spec_name).read_text())
    assert '"format": "password"' not in json.dumps(spec, indent=4)
