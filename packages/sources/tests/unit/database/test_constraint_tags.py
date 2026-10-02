# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ``@pk`` / ``@fk(...)`` column-comment constraint-tag parser.

`constraint_tags.py` is the only place declared keys can enter discovery for a
federated connector (the federation protocol has no constraint field), and a
reference connector's encoder generates against the same grammar — so the
contract is pinned here directly, independent of any connector.
"""

from __future__ import annotations

import logging

from coa_common.domain_models import EnrichmentSource
from coa_sources.database.connectors.constraint_tags import (
    ParsedFk,
    assemble_constraints,
    parse_comment,
)


class TestParseComment:
    def test_plain_comment_no_tags_is_unchanged(self):
        parsed = parse_comment("Customer identifier")
        assert parsed.description == "Customer identifier"
        assert parsed.is_pk_member is False
        assert parsed.foreign_keys == ()

    def test_pk_tag_is_detected_and_stripped(self):
        parsed = parse_comment("Customer identifier @pk")
        assert parsed.description == "Customer identifier"
        assert parsed.is_pk_member is True
        assert parsed.foreign_keys == ()

    def test_single_fk_tag_is_parsed_and_stripped(self):
        parsed = parse_comment("Parent order @fk(orders.order_id)")
        assert parsed.description == "Parent order"
        assert parsed.is_pk_member is False
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)

    def test_multiple_tags_on_one_comment(self):
        parsed = parse_comment("Customer identifier @pk @fk(customers.customer_id)")
        assert parsed.description == "Customer identifier"
        assert parsed.is_pk_member is True
        assert parsed.foreign_keys == (ParsedFk(target_table="customers", target_column="customer_id"),)

    def test_two_fk_tags_on_one_comment(self):
        # A column can reference more than one parent; each tag stands alone.
        parsed = parse_comment("Region key @fk(regions.country) @fk(countries.iso_code)")
        assert parsed.description == "Region key"
        assert parsed.foreign_keys == (
            ParsedFk(target_table="regions", target_column="country"),
            ParsedFk(target_table="countries", target_column="iso_code"),
        )

    def test_empty_comment(self):
        parsed = parse_comment("")
        assert parsed.description == ""
        assert parsed.is_pk_member is False
        assert parsed.foreign_keys == ()

    def test_missing_comment_is_not_fatal(self):
        # DESCRIBE reports no comment as an absent value, not an empty string.
        parsed = parse_comment(None)
        assert parsed.description == ""
        assert parsed.is_pk_member is False
        assert parsed.foreign_keys == ()

    def test_tag_only_comment_cleans_to_empty_string_not_whitespace(self):
        assert parse_comment("@pk").description == ""
        assert parse_comment("  @pk  ").description == ""
        assert parse_comment("@fk(orders.order_id)").description == ""
        assert parse_comment("@pk @fk(orders.order_id)").description == ""

    def test_tag_mid_sentence_is_honoured_and_leaves_no_whitespace_seam(self):
        parsed = parse_comment("@pk Customer identifier, unique")
        assert parsed.description == "Customer identifier, unique"
        assert parsed.is_pk_member is True

        parsed = parse_comment("Customer @pk identifier")
        assert parsed.description == "Customer identifier"
        assert parsed.is_pk_member is True

    def test_whitespace_inside_human_text_is_preserved(self):
        assert parse_comment("Customer  identifier @pk").description == "Customer  identifier"

    def test_extra_leading_qualification_is_tolerated(self):
        # Mirrors parse_referred_column: the last two dotted segments win.
        for reference in ("orders.order_id", "public.orders.order_id", "db.public.orders.order_id"):
            parsed = parse_comment(f"Parent order @fk({reference})")
            assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)

    def test_sentence_period_after_the_closing_bracket_is_prose(self):
        # The bracket ends the tag, so a trailing sentence period needs no special
        # tolerance inside the reference — it is simply text after the tag. (The
        # pre-bracket grammar had to rstrip(".") to survive this, which is exactly
        # what made "@fk(orders.)" indistinguishable from a usable reference.)
        parsed = parse_comment("Parent order @fk(orders.order_id).")
        assert parsed.description == "Parent order."
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)

    def test_reference_ends_at_prose_punctuation(self):
        parsed = parse_comment("Parent order @fk(orders.order_id), nullable")
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)
        assert parsed.description == "Parent order, nullable"

    def test_repeated_identical_fk_tag_is_deduplicated(self):
        parsed = parse_comment("Parent order @fk(orders.order_id) @fk(orders.order_id)")
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)

    def test_repeated_pk_tag_is_idempotent(self):
        parsed = parse_comment("Key @pk @pk")
        assert parsed.description == "Key"
        assert parsed.is_pk_member is True


class TestMalformedTags:
    def test_malformed_fk_tags_are_left_intact_and_reported_once(self, caplog):
        malformed = [
            "@fk()",  # nothing at all between the brackets
            "@fk(orders)",  # no column: cannot build a join condition
            "@fk(.order_id)",  # no table
            "@fk(orders.)",  # no column
            "@fk(orders..order_id)",  # empty segment
            "@fk(orders.id",  # no closing bracket
            '@fk("orders.id)',  # no closing quote
            '@fk("t")',  # one quoted segment: a table with no column
            '@fk(""."col")',  # an explicitly empty segment names no table
            '@fk("t".."c")',  # ".." is not a segment separator
            "@fk(orders.order id)",  # a space in a bare segment: quote it
            "@fk(orders.order_id, nullable)",  # prose let inside the brackets
            '@fk("a"b.c)',  # a segment half quoted and half not
            "@fk(orders.total(usd))",  # an unquoted "(" is not an identifier
        ]
        for tag in malformed:
            with caplog.at_level(logging.WARNING):
                caplog.clear()
                parsed = parse_comment(f"Parent order {tag}")
            assert parsed.foreign_keys == (), tag
            assert parsed.is_pk_member is False, tag
            # Strip-iff-understood: a tag the parser could not act on stays in the
            # text verbatim — the author's only visible signal, since the warning
            # lands in Orion's logs rather than theirs — and is reported once.
            assert parsed.description == f"Parent order {tag}", tag
            assert [record.message for record in caplog.records] == ["constraint_tag_malformed"], tag

    def test_malformed_fk_reasons_are_specific(self, caplog):
        # The warning reaches operators, not the connector author, so the reason
        # has to name the rule that was broken.
        reasons = {
            "@fk()": "fk_operand_empty_identifier",
            "@fk(orders.)": "fk_operand_empty_identifier",
            '@fk(."col")': "fk_operand_empty_identifier",
            '@fk(""."col")': "fk_operand_empty_identifier",
            "@fk(orders..order_id)": "fk_operand_empty_identifier",
            "@fk(orders)": "fk_operand_not_table_dot_column",
            '@fk("t")': "fk_operand_not_table_dot_column",
            "@fk(orders.id": "fk_operand_unterminated_bracket",
            '@fk("orders.id)': "fk_operand_unterminated_quote",
            "@fk(orders.order id)": "fk_operand_segment_requires_quoting",
            "@fk(orders.total(usd))": "fk_operand_segment_requires_quoting",
            '@fk("a"b.c)': "fk_operand_segment_partially_quoted",
            '@fk(a"b".c)': "fk_operand_segment_partially_quoted",
        }
        for tag, reason in reasons.items():
            with caplog.at_level(logging.WARNING):
                caplog.clear()
                parse_comment(f"Parent order {tag}")
            assert [record.reason for record in caplog.records] == [reason], tag

    def test_malformed_fk_tag_is_reported_once_not_twice(self, caplog):
        # A malformed tag stays in the text, so it must not then be re-reported
        # by the near-miss scan under a second, vaguer event name.
        with caplog.at_level(logging.WARNING):
            parse_comment("Parent order @fk(orders)")
        messages = [record.message for record in caplog.records]
        assert messages == ["constraint_tag_malformed"]

    def test_an_unterminated_tag_does_not_swallow_a_later_tag(self, caplog):
        # An unclosed bracket has no discoverable end, so the parser resumes right
        # after the broken tag's own name rather than treating the rest of the
        # comment as its operand: one warning, and the "@pk" still lands.
        with caplog.at_level(logging.WARNING):
            parsed = parse_comment("Key @fk(orders.id @pk")
        assert parsed.is_pk_member is True
        assert parsed.foreign_keys == ()
        assert parsed.description == "Key @fk(orders.id"
        assert [record.message for record in caplog.records] == ["constraint_tag_malformed"]

    def test_tags_are_case_sensitive_and_near_misses_are_logged(self, caplog):
        with caplog.at_level(logging.WARNING):
            parsed = parse_comment("Customer identifier @PK")
        assert parsed.is_pk_member is False
        assert parsed.description == "Customer identifier @PK"
        assert "constraint_tag_unrecognised" in caplog.text

        with caplog.at_level(logging.WARNING):
            caplog.clear()
            parsed = parse_comment("Parent order @FK(orders.order_id)")
        assert parsed.foreign_keys == ()
        assert parsed.description == "Parent order @FK(orders.order_id)"
        assert "constraint_tag_unrecognised" in caplog.text

    def test_retired_equals_spelling_is_a_near_miss(self, caplog):
        # "@fk=orders.order_id" was the earlier spelling and is now no tag at all.
        # It is a plausible thing to write from memory, so it is diagnosed rather
        # than read as prose — and, being unrecognised, it is left in the text.
        for tag in ("@fk=orders.order_id", '@fk="orders"."order_id"'):
            with caplog.at_level(logging.WARNING):
                caplog.clear()
                parsed = parse_comment(f"Parent order {tag}")
            assert parsed.foreign_keys == (), tag
            assert parsed.description == f"Parent order {tag}", tag
            assert [record.message for record in caplog.records] == ["constraint_tag_unrecognised"], tag
            assert [record.reason for record in caplog.records] == ["fk_equals_spelling_retired"], tag

    def test_fk_tag_without_an_operand_is_a_near_miss(self, caplog):
        with caplog.at_level(logging.WARNING):
            parsed = parse_comment("Parent order @fk orders.order_id")
        assert parsed.foreign_keys == ()
        assert parsed.description == "Parent order @fk orders.order_id"
        assert "constraint_tag_unrecognised" in caplog.text

    def test_pk_tag_with_an_operand_is_a_near_miss(self, caplog):
        # "@pk" takes no operand, bracketed or otherwise; guessing at the intent
        # would strip text the parser does not understand.
        for tag in ("@pk=customer_id", "@pk(customer_id)"):
            with caplog.at_level(logging.WARNING):
                caplog.clear()
                parsed = parse_comment(f"Key {tag}")
            assert parsed.is_pk_member is False, tag
            assert parsed.description == f"Key {tag}", tag
            assert [record.message for record in caplog.records] == ["constraint_tag_unrecognised"], tag
            assert [record.reason for record in caplog.records] == ["pk_takes_no_operand"], tag

    def test_tag_prefix_inside_a_longer_word_is_not_a_tag(self):
        parsed = parse_comment("See @pkey_note and @fkeys")
        assert parsed.is_pk_member is False
        assert parsed.foreign_keys == ()
        assert parsed.description == "See @pkey_note and @fkeys"


class TestTagBoundaries:
    def test_address_in_prose_is_not_a_pk_tag(self, caplog):
        # "bob@pk.example.com" is an address, not a tag. Reading it as one would
        # declare a primary key nobody asked for and mangle the description.
        with caplog.at_level(logging.WARNING):
            parsed = parse_comment("Owner bob@pk.example.com")
        assert parsed.is_pk_member is False
        assert parsed.description == "Owner bob@pk.example.com"
        assert caplog.records == []

        parsed = parse_comment("Contact team-data@pk.amazon.com for access")
        assert parsed.is_pk_member is False
        assert parsed.description == "Contact team-data@pk.amazon.com for access"

    def test_address_in_prose_is_not_an_fk_tag(self, caplog):
        # The left-boundary rule holds for the bracketed form and for the retired
        # spelling alike: neither is a tag when it follows an identifier character.
        for comment in ("Ask bob@fk(orders.order_id)", "Ask bob@fk=orders.order_id"):
            with caplog.at_level(logging.WARNING):
                caplog.clear()
                parsed = parse_comment(comment)
            assert parsed.foreign_keys == (), comment
            assert parsed.description == comment, comment
            assert caplog.records == [], comment

    def test_tag_after_non_identifier_punctuation_is_still_a_tag(self):
        parsed = parse_comment("Customer id (@pk)")
        assert parsed.is_pk_member is True

    def test_reference_containing_a_hyphen_is_expressible(self):
        # Glue table names may contain hyphens; Athena reaches them by quoting.
        parsed = parse_comment("Parent @fk(sales-eu.order_id)")
        assert parsed.foreign_keys == (ParsedFk(target_table="sales-eu", target_column="order_id"),)

    def test_period_bound_to_following_prose_is_resolved_by_the_bracket(self):
        # This was the pre-bracket grammar's documented limitation:
        # "@fk=orders.order_id.See notes" resolved to order_id.See, and could not
        # be told apart from the legitimate qualification in
        # "@fk=public.orders.order_id". The closing bracket ends the tag, so the
        # period and the word after it are prose by construction.
        parsed = parse_comment("Parent order @fk(orders.order_id).See notes")
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)
        assert parsed.description == "Parent order.See notes"

    def test_prose_may_follow_the_closing_bracket_immediately(self):
        # No separator at all: only the whitespace PRECEDING a tag is consumed, so
        # the text after the bracket abuts the text before the tag.
        parsed = parse_comment("Parent order @fk(orders.order_id)See notes")
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)
        assert parsed.description == "Parent orderSee notes"

        parsed = parse_comment("@fk(orders.order_id)See notes")
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)
        assert parsed.description == "See notes"

    def test_tag_inside_prose_brackets_ends_at_its_own_bracket(self):
        # The tag's terminator is the first ")" outside a quoted segment, so a tag
        # written inside parenthesised prose closes itself and leaves the outer
        # bracket alone.
        parsed = parse_comment("Parent (see @fk(orders.order_id))")
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)
        assert parsed.description == "Parent (see)"

    def test_surviving_ambiguity_a_dotted_bare_word_inside_the_brackets(self):
        # The one ambiguity the bracket does not remove, pinned so it cannot drift
        # unnoticed while the spec says otherwise: a third segment is legitimate
        # qualification ("db.orders.order_id"), so a bare word attached by a dot
        # INSIDE the operand reads as one. It takes prose written inside the
        # brackets to hit, and only an unbroken word gets through — the usual
        # shapes of the mistake carry a space or punctuation and are rejected.
        parsed = parse_comment("Parent order @fk(orders.order_id.see)")
        assert parsed.foreign_keys == (ParsedFk(target_table="order_id", target_column="see"),)

        parsed = parse_comment("Parent order @fk(orders.order_id.See notes)")
        assert parsed.foreign_keys == ()
        assert parsed.description == "Parent order @fk(orders.order_id.See notes)"


class TestBracketedReferences:
    """Quoting inside the operand: ``@fk("my table"."first name")``.

    A bare segment cannot express a name containing a space or a dot, and spaced
    column names are routine in Glue- and CSV-derived tables — so quoting is
    available per segment, for exactly the segments that need it, and the
    reference connector's encoder quotes on that basis.
    """

    def test_quoted_reference_is_equivalent_to_the_bare_spelling(self):
        parsed = parse_comment('Parent order @fk("orders"."order_id")')
        assert parsed.description == "Parent order"
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)

    def test_spaced_table_name_with_unspaced_column(self):
        parsed = parse_comment('Parent order @fk("my table"."order_id")')
        assert parsed.description == "Parent order"
        assert parsed.foreign_keys == (ParsedFk(target_table="my table", target_column="order_id"),)

    def test_unspaced_table_name_with_spaced_column(self):
        parsed = parse_comment('Order date @fk("orders"."Order Date")')
        assert parsed.description == "Order date"
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="Order Date"),)

    def test_name_containing_a_dot_is_expressible_only_when_quoted(self):
        # One reason quoting exists at all: a dot inside a name has no bare
        # spelling, since the dot is the segment separator.
        parsed = parse_comment('Parent @fk("a.b"."c")')
        assert parsed.foreign_keys == (ParsedFk(target_table="a.b", target_column="c"),)

    def test_name_containing_a_closing_bracket_is_expressible_when_quoted(self):
        # The other reason: the tag's terminator is the first ")" OUTSIDE a quoted
        # segment, so a quoted name may contain the terminator character itself.
        parsed = parse_comment('Amount @fk("total (usd)"."x")')
        assert parsed.description == "Amount"
        assert parsed.foreign_keys == (ParsedFk(target_table="total (usd)", target_column="x"),)

        # And mixed with a bare final segment, which is where a naive scan for the
        # first ")" would stop early.
        parsed = parse_comment('Amount @fk("total (usd)".x) per order')
        assert parsed.description == "Amount per order"
        assert parsed.foreign_keys == (ParsedFk(target_table="total (usd)", target_column="x"),)

    def test_embedded_double_quote_is_written_doubled(self):
        # SQL's escape: "" inside a segment is one literal double quote.
        parsed = parse_comment('Parent @fk("he said ""hi"""."x")')
        assert parsed.foreign_keys == (ParsedFk(target_table='he said "hi"', target_column="x"),)

        # The same name ending in a quote, mixed with a bare column. (This shape
        # cannot be written in a module docstring — the closing quote would run
        # into the docstring's own delimiter — so the grammar's support for it is
        # pinned here.)
        parsed = parse_comment('Parent @fk("he said ""hi""".x)')
        assert parsed.foreign_keys == (ParsedFk(target_table='he said "hi"', target_column="x"),)

        parsed = parse_comment('Parent @fk("say ""hi"" now".x)')
        assert parsed.foreign_keys == (ParsedFk(target_table='say "hi" now', target_column="x"),)

    def test_quoting_is_per_segment_and_independent(self):
        # The all-or-nothing rule is gone: quote what needs it, leave the rest.
        for reference in ('"orders"."order_id"', '"orders".order_id', 'orders."order_id"', "orders.order_id"):
            parsed = parse_comment(f"Parent order @fk({reference})")
            assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),), reference
            assert parsed.description == "Parent order", reference

    def test_mixed_quoting_with_three_segments_resolves_to_the_last_two(self):
        # The case the all-or-nothing rule mis-targeted: the reference used to end
        # at the last closing quote, yielding a usable-looking key to db.orders
        # with ".order_id" left as prose. Inside a bracket it is unambiguous.
        parsed = parse_comment('Parent order @fk("db"."orders".order_id)')
        assert parsed.description == "Parent order"
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)

        parsed = parse_comment('Parent order @fk(db."orders"."order id")')
        assert parsed.description == "Parent order"
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order id"),)

    def test_extra_leading_qualification_is_tolerated_when_quoted(self):
        # Same rule as the bare form: the last two segments are TABLE.COLUMN.
        for reference in (
            '"orders"."order_id"',
            '"public"."orders"."order_id"',
            '"db"."public"."orders"."order_id"',
        ):
            parsed = parse_comment(f"Parent order @fk({reference})")
            assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),), reference

    def test_whitespace_inside_the_brackets_is_padding(self):
        parsed = parse_comment("Parent order @fk( orders . order_id )")
        assert parsed.description == "Parent order"
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)

        parsed = parse_comment('Parent order @fk( "my table" . order_id )')
        assert parsed.foreign_keys == (ParsedFk(target_table="my table", target_column="order_id"),)

    def test_quoted_tag_mid_sentence_leaves_no_whitespace_seam(self):
        parsed = parse_comment('Country @fk("my table"."order id") of the region')
        assert parsed.description == "Country of the region"
        assert parsed.foreign_keys == (ParsedFk(target_table="my table", target_column="order id"),)

    def test_two_quoted_tags_on_one_comment(self):
        parsed = parse_comment('Region key @fk("regions"."country") @fk("countries"."iso code")')
        assert parsed.description == "Region key"
        assert parsed.foreign_keys == (
            ParsedFk(target_table="regions", target_column="country"),
            ParsedFk(target_table="countries", target_column="iso code"),
        )

    def test_quoted_and_bare_spellings_of_one_target_deduplicate(self):
        # Dedup compares the DECODED (table, column) pair, not the tag text: the
        # two spellings name the same target, and keeping both would store two
        # byte-identical ForeignKey rows.
        parsed = parse_comment('Parent order @fk(orders.order_id) @fk("orders"."order_id")')
        assert parsed.description == "Parent order"
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)

    def test_child_column_name_comes_from_describe_not_from_the_tag(self):
        # The tag sits in the child column's own comment, so a spaced CHILD name
        # never needs quoting: only the parent TABLE.COLUMN is spelled in the tag.
        columns = [("Order Date", parse_comment('Order date @fk("my orders"."Order Date")'))]
        primary_key, foreign_keys = assemble_constraints(columns)
        assert primary_key is None
        assert [(fk.column, fk.target_table, fk.target_column) for fk in foreign_keys] == [
            ("Order Date", "my orders", "Order Date")
        ]


class TestAssembleConstraints:
    def test_no_tags_yields_no_constraints(self):
        columns = [("customer_id", parse_comment("Customer identifier"))]
        primary_key, foreign_keys = assemble_constraints(columns)
        # None, not an empty PrimaryKey: the table declares no key at all.
        assert primary_key is None
        assert foreign_keys == []

    def test_single_column_primary_key_is_deterministic(self):
        columns = [
            ("customer_id", parse_comment("Customer identifier @pk")),
            ("name", parse_comment("Customer name")),
        ]
        primary_key, foreign_keys = assemble_constraints(columns)
        assert primary_key is not None
        assert primary_key.columns == ["customer_id"]
        assert primary_key.source == EnrichmentSource.DETERMINISTIC
        assert primary_key.confidence == 1.0
        assert foreign_keys == []

    def test_composite_primary_key_follows_describe_order(self):
        columns = [
            ("region_country", parse_comment("Country @pk")),
            ("ignored", parse_comment("Not a key")),
            ("region_zone", parse_comment("Zone @pk")),
        ]
        primary_key, _ = assemble_constraints(columns)
        assert primary_key is not None
        assert primary_key.columns == ["region_country", "region_zone"]

    def test_single_column_foreign_key_is_deterministic(self):
        columns = [("order_ref", parse_comment("Parent order @fk(orders.order_id)"))]
        _, foreign_keys = assemble_constraints(columns)
        assert len(foreign_keys) == 1
        fk = foreign_keys[0]
        assert fk.column == "order_ref"
        assert fk.target_table == "orders"
        assert fk.target_column == "order_id"
        assert fk.source == EnrichmentSource.DETERMINISTIC
        assert fk.confidence == 1.0

    def test_composite_fk_is_stored_as_one_row_per_child_column(self):
        # LLD Appendix A1: region_country + region_zone form a composite FK to
        # regions(country, zone), expressed as one @fk(...) per child column and
        # stored ungrouped — exactly as the JDBC path flattens composites.
        columns = [
            ("region_country", parse_comment("Country of the sales region @fk(regions.country)")),
            ("region_zone", parse_comment("Zone within the country @fk(regions.zone)")),
        ]
        primary_key, foreign_keys = assemble_constraints(columns)
        assert primary_key is None
        assert [(fk.column, fk.target_table, fk.target_column) for fk in foreign_keys] == [
            ("region_country", "regions", "country"),
            ("region_zone", "regions", "zone"),
        ]

    def test_appendix_a1_worked_example(self):
        raw = {
            "customer_id": "Customer identifier @pk",
            "order_ref": "Parent order @fk(orders.order_id)",
            "region_country": "Country of the sales region @fk(regions.country)",
            "region_zone": "Zone within the country @fk(regions.zone)",
        }
        columns = [(name, parse_comment(comment)) for name, comment in raw.items()]

        assert [parsed.description for _, parsed in columns] == [
            "Customer identifier",
            "Parent order",
            "Country of the sales region",
            "Zone within the country",
        ]

        primary_key, foreign_keys = assemble_constraints(columns)
        assert primary_key is not None
        assert primary_key.columns == ["customer_id"]
        assert [(fk.column, fk.target_table, fk.target_column) for fk in foreign_keys] == [
            ("order_ref", "orders", "order_id"),
            ("region_country", "regions", "country"),
            ("region_zone", "regions", "zone"),
        ]

    def test_column_can_be_both_pk_member_and_fk_child(self):
        columns = [("order_ref", parse_comment("Parent order @pk @fk(orders.order_id)"))]
        primary_key, foreign_keys = assemble_constraints(columns)
        assert primary_key is not None
        assert primary_key.columns == ["order_ref"]
        assert len(foreign_keys) == 1

    def test_no_columns_yields_no_constraints(self):
        assert assemble_constraints([]) == (None, [])


class TestNotNullTag:
    """``@notnull`` — operand-free like ``@pk``, and additive by construction.

    Absence must keep meaning *unknown* (and so nullable), because every connector deployed
    before this tag existed emits none. The tests below assert both that the tag works and
    that its absence changes nothing.
    """

    def test_notnull_tag_is_detected_and_stripped(self):
        parsed = parse_comment("Customer identifier @notnull")
        assert parsed.description == "Customer identifier"
        assert parsed.is_not_null is True

    def test_absence_means_nullable_not_unknown_shaped_as_false(self):
        # The backward-compatibility guarantee: no tag becomes nullable=True downstream,
        # exactly what the discovery path hardcoded before this tag existed.
        assert parse_comment("Customer identifier").is_not_null is False
        assert parse_comment("").is_not_null is False
        assert parse_comment(None).is_not_null is False

    def test_notnull_combines_with_the_other_tags(self):
        parsed = parse_comment("Parent order @pk @notnull @fk(orders.order_id)")
        assert parsed.description == "Parent order"
        assert parsed.is_pk_member is True
        assert parsed.is_not_null is True
        assert parsed.foreign_keys == (ParsedFk(target_table="orders", target_column="order_id"),)

    def test_a_tag_only_comment_cleans_to_empty(self):
        parsed = parse_comment("@notnull")
        assert parsed.description == ""
        assert parsed.is_not_null is True

    def test_wrong_case_is_a_near_miss_and_is_left_in_place(self, caplog):
        with caplog.at_level(logging.WARNING):
            parsed = parse_comment("Customer identifier @NOTNULL")
        assert parsed.is_not_null is False
        assert parsed.description == "Customer identifier @NOTNULL"
        assert "constraint_tag_unrecognised" in caplog.text

    def test_an_operand_makes_it_a_near_miss(self, caplog):
        # The leftover text is the author's only observable feedback channel.
        for tag in ("@notnull=true", "@notnull(customer_id)"):
            with caplog.at_level(logging.WARNING):
                caplog.clear()
                parsed = parse_comment(f"Key {tag}")
            assert parsed.is_not_null is False, tag
            assert parsed.description == f"Key {tag}", tag
            assert [record.message for record in caplog.records] == ["constraint_tag_unrecognised"], tag
            assert [record.reason for record in caplog.records] == ["notnull_takes_no_operand"], tag

    def test_a_longer_word_starting_the_same_way_is_not_a_tag(self, caplog):
        # "@notnullable" is a plausible typo for the REJECTED spelling, so it is reported
        # rather than acted on. "@note" stays prose, which is what keeps the change
        # additive for comments already in the field.
        with caplog.at_level(logging.WARNING):
            parsed = parse_comment("Value @notnullable")
        assert parsed.is_not_null is False
        assert parsed.description == "Value @notnullable"
        assert "constraint_tag_unrecognised" in caplog.text

        with caplog.at_level(logging.WARNING):
            caplog.clear()
            parsed = parse_comment("See @note and @notes")
        assert parsed.is_not_null is False
        assert parsed.description == "See @note and @notes"
        assert caplog.records == []

    def test_an_email_address_in_prose_is_not_a_tag(self):
        # The left-boundary rule: reading an address as a tag would both mark the column
        # non-nullable and mangle the description.
        parsed = parse_comment("Owner bob@notnull.example.com")
        assert parsed.is_not_null is False
        assert parsed.description == "Owner bob@notnull.example.com"

    def test_strip_is_iff_understood(self):
        # The property the grammar rests on: text is removed only when the parser acted on
        # it.
        assert parse_comment("A @notnull B").description == "A B"
        assert parse_comment("A @notnull=1 B").description == "A @notnull=1 B"
