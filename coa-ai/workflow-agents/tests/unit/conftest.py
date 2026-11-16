# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Suite-wide setup, run before any test module is imported."""

from __future__ import annotations

import os

# LOG_LEVEL is a common shell variable. server.py reads it at import (an
# invalid value fails there), and the config tests assert the default, so a
# developer's own value must not leak into the suite. Tests that need a level
# set it with monkeypatch.
for _name in ("LOG_LEVEL",):
    os.environ.pop(_name, None)
