# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Smithy-to-CM shape helpers."""

from __future__ import annotations

import pytest
from coa_common.smithy_shapes import normalize_dimensions


@pytest.mark.unit
class TestNormalizeDimensions:
    """The shape contract between the Smithy DimensionFilterList wire form
    and CM's Tier-1 ``substitute_dimensions`` (which calls ``.items()``).

    A regression here is not silent: every dimensioned Tier-1 metric query
    500s with ``AttributeError`` — pin the mapping shape hard.
    """

    def test_list_of_filters_becomes_mapping(self):
        """The primary wire shape: Smithy ``[{name, value}, ...]`` normalises
        to the ``{name: value}`` mapping CM's Tier-1 resolver consumes."""
        assert normalize_dimensions([{"name": "region", "value": "us-east"}]) == {"region": "us-east"}

    def test_multiple_filters(self):
        assert normalize_dimensions(
            [
                {"name": "region", "value": "us-east"},
                {"name": "year", "value": 2026},
            ]
        ) == {"region": "us-east", "year": 2026}

    def test_operator_field_is_dropped(self):
        """The Smithy shape may carry an ``operator``. CM's ``substitute_dimensions``
        supports equality only; forwarding operator would silently apply the
        wrong filter. Drop it here rather than pretending to support it."""
        assert normalize_dimensions([{"name": "region", "value": "us-east", "operator": "!="}]) == {"region": "us-east"}

    def test_malformed_entry_without_name_is_dropped(self):
        """A single malformed entry must not 500 the whole query. Skip it
        silently and let the rest through."""
        assert normalize_dimensions(
            [
                {"name": "region", "value": "us-east"},
                {"value": "no-name-here"},
                {"name": "year", "value": 2026},
            ]
        ) == {"region": "us-east", "year": 2026}

    def test_dict_is_passed_through(self):
        """A caller that has already normalized (e.g. an internal test or a
        future consumer) is idempotent."""
        assert normalize_dimensions({"region": "us-east"}) == {"region": "us-east"}

    def test_none_returns_empty_dict(self):
        """The call site should short-circuit on empty, but ``None`` in
        must not blow up."""
        assert normalize_dimensions(None) == {}

    def test_empty_list_returns_empty_dict(self):
        assert normalize_dimensions([]) == {}

    def test_non_list_non_dict_returns_empty_dict(self):
        """Anything else — a scalar, a string, a set — is not a valid
        DimensionFilterList. Return empty so the call site skips forwarding
        the field entirely rather than smuggling garbage into CM."""
        assert normalize_dimensions("region=us-east") == {}
        assert normalize_dimensions(42) == {}
