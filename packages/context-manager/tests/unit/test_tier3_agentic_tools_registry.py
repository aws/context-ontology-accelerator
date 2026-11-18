# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Agentic Retriever Tool contract and ToolRegistry."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from coa_serve.tier3.agentic.tools import Tool, ToolRegistry, ToolResult
from coa_serve.tier3.agentic.tools.base import (
    Tool as DirectTool,
)
from coa_serve.tier3.agentic.tools.registry import (
    ToolRegistry as DirectRegistry,
)

_UNSET = object()


class StubTool:
    """A minimal, valid Tool: four interface fields + an async ``invoke``.

    Omitted arguments fall back to valid defaults; an explicit ``None`` reaches the field.
    """

    def __init__(
        self,
        name=_UNSET,
        description=_UNSET,
        input_schema=_UNSET,
        output_schema=_UNSET,
        result: ToolResult | None = None,
    ) -> None:
        self.name = "strategy:entity_based" if name is _UNSET else name
        self.description = "Entity-anchored retrieval strategy." if description is _UNSET else description
        self.input_schema = {"query": "str"} if input_schema is _UNSET else input_schema
        self.output_schema = {"chunks": "list"} if output_schema is _UNSET else output_schema
        self._result = result or ToolResult()

    async def invoke(self, *, namespace: str, **kwargs) -> ToolResult:
        return self._result


@pytest.mark.unit
class TestToolResult:
    def test_empty_defaults(self):
        result = ToolResult()
        assert result.items == ()
        assert result.chunks == ()
        assert result.entities == ()
        assert result.detail == ""
        assert result.produced_directions == ()

    def test_is_frozen(self):
        result = ToolResult(detail="x")
        with pytest.raises(FrozenInstanceError):
            result.detail = "y"  # type: ignore[misc]

    def test_carries_provided_values(self):
        result = ToolResult(
            items=({"k": "v"},),
            detail="ok",
            produced_directions=("Walmart financials",),
        )
        assert result.items == ({"k": "v"},)
        assert result.detail == "ok"
        assert result.produced_directions == ("Walmart financials",)


@pytest.mark.unit
class TestReexports:
    def test_package_reexports_module_symbols_unchanged(self):
        assert Tool is DirectTool
        assert ToolRegistry is DirectRegistry


@pytest.mark.unit
class TestRegisterAndLookup:
    def test_register_then_get_and_names(self):
        registry = ToolRegistry()
        tool = StubTool(name="strategy:entity_based")
        registry.register(tool)

        assert registry.get("strategy:entity_based") is tool
        assert registry.names() == ["strategy:entity_based"]

    def test_get_unknown_returns_none(self):
        registry = ToolRegistry()
        assert registry.get("does_not_exist") is None

    def test_names_preserve_registration_order(self):
        registry = ToolRegistry()
        registry.register(StubTool(name="a"))
        registry.register(StubTool(name="b"))
        registry.register(StubTool(name="c"))
        assert registry.names() == ["a", "b", "c"]

    def test_stub_tool_satisfies_runtime_tool_protocol(self):
        """The structural Tool contract is satisfied by the four fields +
        ``invoke`` — confirming the test stand-in is a faithful Tool."""
        assert isinstance(StubTool(), Tool)


@pytest.mark.unit
class TestMissingFieldRejection:
    """Registration rejects a Tool missing any of the four required fields and
    excludes it from selection."""

    @pytest.mark.parametrize("field", ["name", "description"])
    def test_rejects_none_string_field(self, field):
        registry = ToolRegistry()
        tool = StubTool(**{field: None})  # type: ignore[arg-type]
        with pytest.raises(ValueError, match=field):
            registry.register(tool)
        # Excluded from selection.
        assert registry.names() == []

    @pytest.mark.parametrize("field", ["name", "description"])
    def test_rejects_empty_or_whitespace_string_field(self, field):
        registry = ToolRegistry()
        tool = StubTool(**{field: "   "})
        with pytest.raises(ValueError, match=field):
            registry.register(tool)
        assert registry.names() == []

    @pytest.mark.parametrize("field", ["input_schema", "output_schema"])
    def test_rejects_none_schema_field(self, field):
        registry = ToolRegistry()
        tool = StubTool(**{field: None})
        with pytest.raises(ValueError, match=field):
            registry.register(tool)
        assert registry.names() == []

    @pytest.mark.parametrize("field", ["input_schema", "output_schema"])
    def test_rejects_non_dict_schema_field(self, field):
        registry = ToolRegistry()
        tool = StubTool(**{field: "not-a-dict"})  # type: ignore[arg-type]
        with pytest.raises(ValueError, match=field):
            registry.register(tool)
        assert registry.names() == []

    def test_rejects_attribute_entirely_absent(self):
        """A bare object lacking all four fields is rejected, and the error
        names every missing field."""

        class NotATool:
            async def invoke(self, *, namespace: str, **kwargs) -> ToolResult:
                return ToolResult()

        registry = ToolRegistry()
        with pytest.raises(ValueError) as exc_info:
            registry.register(NotATool())  # type: ignore[arg-type]
        message = str(exc_info.value)
        for field in ("name", "description", "input_schema", "output_schema"):
            assert field in message
        assert registry.names() == []

    def test_error_lists_all_missing_fields_together(self):
        registry = ToolRegistry()
        tool = StubTool(name=None, output_schema=None)  # type: ignore[arg-type]
        with pytest.raises(ValueError) as exc_info:
            registry.register(tool)
        message = str(exc_info.value)
        assert "name" in message
        assert "output_schema" in message

    def test_empty_dict_schema_is_accepted(self):
        """An empty schema dict is a valid declaration (e.g. a no-input tool) and
        is NOT treated as missing."""
        registry = ToolRegistry()
        registry.register(StubTool(name="no_inputs", input_schema={}))
        assert registry.get("no_inputs") is not None


@pytest.mark.unit
class TestDuplicateNameRejection:
    """A duplicate name is rejected; the original Tool is retained unchanged."""

    def test_duplicate_name_rejected_and_original_retained(self):
        registry = ToolRegistry()
        original = StubTool(name="graph_traversal", description="original")
        registry.register(original)

        duplicate = StubTool(name="graph_traversal", description="replacement")
        with pytest.raises(ValueError, match="graph_traversal"):
            registry.register(duplicate)

        # The original is still the one registered — unchanged.
        assert registry.get("graph_traversal") is original
        assert registry.get("graph_traversal").description == "original"
        assert registry.names() == ["graph_traversal"]


@pytest.mark.unit
class TestSpecs:
    """``specs()`` returns name + description + schemas for the planner."""

    def test_specs_contains_name_description_and_schemas(self):
        registry = ToolRegistry()
        registry.register(
            StubTool(
                name="strategy:entity_based",
                description="Entity-anchored retrieval.",
                input_schema={"query": "str", "top_k": "int"},
                output_schema={"chunks": "list", "entities": "list"},
            )
        )

        specs = registry.specs()
        assert len(specs) == 1
        spec = specs[0]
        assert spec == {
            "name": "strategy:entity_based",
            "description": "Entity-anchored retrieval.",
            "input_schema": {"query": "str", "top_k": "int"},
            "output_schema": {"chunks": "list", "entities": "list"},
        }

    def test_specs_preserve_registration_order(self):
        registry = ToolRegistry()
        registry.register(StubTool(name="a"))
        registry.register(StubTool(name="b"))
        assert [s["name"] for s in registry.specs()] == ["a", "b"]

    def test_specs_empty_when_no_tools(self):
        assert ToolRegistry().specs() == []
