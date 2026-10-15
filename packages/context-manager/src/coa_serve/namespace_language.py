# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The request's namespace-language label preference.

Serve readers (Tier-2 graph tools and T-Box context, Tier-3 ontology and graph
readers) choose one display label per resource from the namespace's immutable
language (namespace-language LLD §2.2.5). The entrypoint resolves that language
once per request, from the namespace item it already reads for the existence
gate, and binds the preference here; readers read it without every call path
threading a parameter through.

This is not a reader locale. Nothing here comes from the request: the question
and answer language stay owned by Serve, and a request cannot change which
label a namespace prefers.

Unbound (a deployment with no namespaces table, or a component used outside a
request) means :meth:`LabelPreference.untagged_first`. A configured namespaces
table whose item is missing or unreadable never reaches the readers: the
entrypoint fails the request first.
"""

from __future__ import annotations

from contextvars import ContextVar

from coa_common.label_language import LabelPreference

_UNBOUND = LabelPreference.untagged_first()

_label_preference: ContextVar[LabelPreference] = ContextVar("coa_serve_label_preference", default=_UNBOUND)


def bind_label_preference(preference: LabelPreference) -> None:
    """Bind the namespace's label preference for the rest of this request's task.

    Tasks spawned afterwards inherit it (``asyncio`` copies the context at task
    creation), so concurrent tier and retriever tasks see the same value.
    """
    _label_preference.set(preference)


def current_label_preference() -> LabelPreference:
    """Return the label preference bound for the current request."""
    return _label_preference.get()


def lang_projection(variable: str) -> str:
    """SPARQL projection binding ``?<variable>Lang`` to the literal's tag.

    The serve graph client flattens bindings to their lexical values, so a
    reader that ranks labels selects the tag explicitly. ``LANG`` of an unbound
    OPTIONAL variable leaves the projection unbound, which reads as untagged.
    """
    return f"(LANG(?{variable}) AS ?{variable}Lang)"


def row_label(row: dict, variable: str) -> tuple[str, str]:
    """Return ``(text, language)`` for ``variable`` from a flattened row."""
    return (str(row.get(variable) or ""), str(row.get(f"{variable}Lang") or ""))
