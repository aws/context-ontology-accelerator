# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Prompt templates for table-level metadata enrichment."""

from __future__ import annotations

from coa_common.bedrock import PromptSegment, prompt_text
from coa_common.domain_models import Column
from coa_common.language_tags import describe_language, needs_language_instruction

SYSTEM_PROMPT = """\
You are a data catalog expert. \
Analyze the provided table schema and generate business metadata as JSON.

Respond ONLY with raw JSON. Do not wrap in markdown code fences or any other formatting.
Output must match this exact structure:
{
  "table": {
    "description": "REQUIRED - a clear business description of what this table represents",
    "synonyms": ["alternative names or abbreviations"],
    "glossary_terms": ["relevant business domain terms"],
    "tags": ["categorization tags"]
  },
  "primary_key": {
    "columns": ["column_name"],
    "confidence": 0.9
  },
  "columns": [
    {
      "name": "column_name",
      "description": "Business description of this column",
      "synonyms": ["alternative names"],
      "glossary_terms": ["relevant terms"],
      "tags": ["tags"],
      "confidence": 0.9
    }
  ]
}

Rules:
- The "Existing metadata" section, when present, lists ONLY the fields that are \
already known. Reproduce those fields unchanged and generate everything else. \
It never means "skip the rest of the output".
- "table.description" is REQUIRED. Generate it unless the "Existing metadata" section \
contains an explicit "description:" entry. A missing table description, an empty \
string, or a null value is an invalid response.
- "columns_with_descriptions" lists columns that already have an authoritative \
description: copy each one verbatim. It says nothing about the table description and \
must not stop you from generating one.
- Always generate synonyms, glossary_terms, and tags for the table, even when the \
description already exists.
- Infer primary key from naming patterns (id, _id, _key suffixes) and data types. \
Preserve a primary key listed in "Existing metadata" as-is.
- Do NOT infer foreign keys — relationship inference is handled in a separate pass.
- confidence should be 0.0-1.0 based on how certain the inference is. Include a \
per-column confidence reflecting how certain you are of that column's generated description.
- Provide descriptions that explain business meaning, not just technical type.
- Keep synonyms and tags concise (max 5 each).
- Include ALL columns from the input in your response. \
If a column has a source comment (shown after "--"), use it as-is for the description.
- If "External evidence" is provided below, use it as authoritative domain knowledge \
to improve both the table description and the column descriptions."""


def build_system_prompt(namespace_language: str) -> str:
    """Return the trusted system prompt for a namespace language.

    An English namespace (primary subtag ``en``) gets :data:`SYSTEM_PROMPT`
    unchanged. Any other language appends a fixed output-language instruction.

    The instruction is application-owned control text, so it lives in the system
    prompt: the table schema, identifiers, comments and external evidence stay
    in the user message, which is what the Bedrock guardrail's prompt-attack
    filter screens. Only the canonical tag and a name from a fixed table are
    interpolated.
    """
    if not needs_language_instruction(namespace_language):
        return SYSTEM_PROMPT
    target = describe_language(namespace_language)
    return (
        SYSTEM_PROMPT + "\n\nOutput language:\n"
        f"- Write every newly generated description, synonym, glossary term, and tag in {target}.\n"
        f"- A synonym is another name a business user would use in {target}. Do not add an English "
        "translation of a table or column name, or an English-style identifier made from it (such as a "
        "snake_case name), as a synonym. An abbreviation or code that the source itself uses may stay as written.\n"
        "- Keep table names, column names, and other identifiers (model numbers, product names, "
        "units, codes) exactly as they appear. Do not translate or transliterate them.\n"
        "- Reproduce existing metadata and source comments unchanged, even when they use another language."
    )


def build_user_prompt_segments(
    table_name: str,
    database: str,
    columns: list[Column],
    *,
    existing_description: str = "",
    existing_pk: list[str] | None = None,
    existing_fks: list[dict] | None = None,
    extra_context: str = "",
) -> list[PromptSegment]:
    """Build the user message as ordered segments with table schema details and existing metadata.

    Everything derived from the source (table and database names, column names,
    types and comments, existing description, keys, external evidence) is a
    guarded segment, so the Bedrock guardrail screens it. The headings and fixed
    instructions written here are unguarded: the guardrail's prompt-attack filter
    otherwise flags the application's own "you MUST generate" wording on a small
    table, where it outweighs the schema. Line formatting next to source data
    stays inside the guarded segment.

    Args:
        table_name: Name of the table being enriched.
        database: Database/schema the table belongs to.
        columns: Columns to describe, including any existing per-column metadata.
        existing_description: Current table description to preserve (not regenerated).
        existing_pk: Known primary-key columns to preserve, if any.
        existing_fks: Known foreign-key relationships to preserve, if any.
        extra_context: Domain knowledge appended as "External evidence" section.

    Returns:
        Ordered segments. Their concatenation is the text the model sees, see
        :func:`build_user_prompt`.
    """
    col_lines = []
    for c in columns:
        line = f"  - {c.name} ({c.data_type})"
        if c.business_metadata.description:
            line += f"  -- {c.business_metadata.description}"
        col_lines.append(line)
    segments = [
        PromptSegment("Table: ", guarded=False),
        PromptSegment(f"{database}.{table_name}"),
        PromptSegment("\n\nColumns:\n", guarded=False),
        PromptSegment("\n".join(col_lines)),
    ]

    existing_parts: list[str] = []
    if existing_description:
        existing_parts.append(f"description: {existing_description}")
    if existing_pk:
        existing_parts.append(f"primary_key: {existing_pk}")
    if existing_fks:
        existing_parts.append(f"foreign_keys: {existing_fks}")

    # Note which columns already have descriptions on business_metadata.
    described = [c.name for c in columns if c.business_metadata.description]
    if described:
        existing_parts.append(f"columns_with_descriptions: {described}")

    if existing_parts:
        segments.append(
            PromptSegment(
                "\n\nExisting metadata (reproduce these fields unchanged; generate everything else):\n",
                guarded=False,
            )
        )
        segments.append(PromptSegment("\n".join(f"  {p}" for p in existing_parts)))

    # State the requirement explicitly rather than relying on the model to
    # infer it from the *absence* of a "description:" entry above. Without
    # this, a populated "Existing metadata" block (e.g. source column comments
    # on every column) reads as "metadata already exists" and the model
    # silently omits table.description while still emitting synonyms/tags.
    if not existing_description:
        segments.append(
            PromptSegment(
                "\n\nREQUIRED: this table has no description yet — you MUST generate a non-empty table.description.",
                guarded=False,
            )
        )

    if extra_context:
        segments.append(PromptSegment("\n\nExternal evidence:\n", guarded=False))
        segments.append(PromptSegment(extra_context))

    return segments


def build_user_prompt(
    table_name: str,
    database: str,
    columns: list[Column],
    *,
    existing_description: str = "",
    existing_pk: list[str] | None = None,
    existing_fks: list[dict] | None = None,
    extra_context: str = "",
) -> str:
    """Return the user message text exactly as the model sees it (see :func:`build_user_prompt_segments`)."""
    return prompt_text(
        build_user_prompt_segments(
            table_name,
            database,
            columns,
            existing_description=existing_description,
            existing_pk=existing_pk,
            existing_fks=existing_fks,
            extra_context=extra_context,
        )
    )
