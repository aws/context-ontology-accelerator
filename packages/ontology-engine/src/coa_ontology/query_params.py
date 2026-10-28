# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Contract checks for enum-valued query parameters on the ontology engine's routes.

A list filter declared in Smithy as an enum (``ProposalStatus``, ``OntologyType``,
``GraphVertexKind``, …) must reject a value outside that enum with a 400, like the
control plane, sources and serve do. Left as a plain ``str``, a typo reads as
"no data": the filter matches nothing and the route returns an empty 200.

The allowed values come from the Smithy-generated enums, so the runtime check and
the model cannot drift apart. The caller's value is never echoed back.
"""

from __future__ import annotations

from enum import Enum

from fastapi import HTTPException


def require_enum_value(
    param: str, value: str | None, enum_cls: type[Enum], *, also_allowed: tuple[str, ...] = ()
) -> None:
    """Raise a 400 when ``value`` is set but is not a member of ``enum_cls``.

    ``None`` and the empty string mean "no filter" (a bare ``?status=`` has always
    been treated as absent), so both pass.

    Args:
        param: Query parameter name as the caller spells it, used in the message.
        value: The raw query parameter value.
        enum_cls: The Smithy-generated enum whose values are allowed.
        also_allowed: Values the service itself stores that the enum does not list,
            so a filter on them keeps working.

    Raises:
        HTTPException: 400 naming the parameter and its allowed values.
    """
    if not value:
        return
    allowed = [str(member.value) for member in enum_cls]
    allowed += [v for v in also_allowed if v not in allowed]
    if value not in allowed:
        raise HTTPException(400, f"{param} must be one of: {', '.join(allowed)}")
