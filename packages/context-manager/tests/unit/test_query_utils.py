# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for query_utils — entity extraction, namespace validation, graph URI template."""

from __future__ import annotations

import unicodedata
from unittest.mock import patch

import pytest
from coa_serve.query_utils import (
    _MAX_INLINED_GRAPHS,
    DEFAULT_GRAPH_URI_TEMPLATE,
    MAX_QUERY_CODEPOINTS,
    MAX_SEARCH_TOKEN_CODEPOINTS,
    QuerySearchPlan,
    build_query_search_plan,
    extract_query_entities,
    get_graph_uri_template,
    graph_scoped_body,
    namespace_graph_prefix,
    object_properties_sparql,
    validate_namespace,
)


@pytest.mark.unit
class TestExtractQueryEntities:
    def test_filters_stop_words(self) -> None:
        entities = extract_query_entities("What is the total claim amount for all customers")
        assert "what" not in entities
        assert "the" not in entities
        assert "claim" in entities
        assert "amount" in entities
        assert "customers" in entities

    def test_filters_short_words(self) -> None:
        entities = extract_query_entities("go to db")
        assert "go" not in entities
        assert "to" not in entities

    def test_max_count(self) -> None:
        entities = extract_query_entities("one two three four five six seven eight nine ten eleven", max_count=3)
        assert len(entities) <= 3

    def test_lowercase_normalization(self) -> None:
        entities = extract_query_entities("Show CLAIMS by CUSTOMER")
        assert "claims" in entities
        assert "customer" in entities

    def test_hyphenated_terms(self) -> None:
        entities = extract_query_entities("check order-id for S3 bucket")
        assert "order-id" in entities


@pytest.mark.unit
class TestValidateNamespace:
    def test_valid_namespace(self) -> None:
        validate_namespace("insurance")
        validate_namespace("my-namespace-01")

    def test_invalid_namespace_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid namespace"):
            validate_namespace("bad namespace!")

    def test_empty_namespace_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid namespace"):
            validate_namespace("")


@pytest.mark.unit
class TestGetGraphUriTemplate:
    def test_default_template(self) -> None:
        result = get_graph_uri_template()
        assert result == DEFAULT_GRAPH_URI_TEMPLATE

    def test_override_takes_precedence(self) -> None:
        result = get_graph_uri_template("custom://template/{namespace}")
        assert result == "custom://template/{namespace}"

    def test_env_var_fallback(self) -> None:
        with patch.dict("os.environ", {"GRAPH_URI_TEMPLATE": "env://t/{namespace}"}):
            result = get_graph_uri_template()
        assert result == "env://t/{namespace}"

    def test_override_beats_env_var(self) -> None:
        with patch.dict("os.environ", {"GRAPH_URI_TEMPLATE": "env://t/{namespace}"}):
            result = get_graph_uri_template("override://t/{namespace}")
        assert result == "override://t/{namespace}"


@pytest.mark.unit
class TestExtractQueryEntitiesIsScriptAgnostic:
    """Regression coverage for grapheme-preserving multilingual search terms."""

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            pytest.param("Wärmepumpe Betriebsführung", ["wärmepumpe", "betriebsführung"], id="de"),
            pytest.param("Régulation thermique", ["régulation", "thermique"], id="fr"),
            pytest.param("Especificación técnica", ["especificación", "técnica"], id="es"),
            pytest.param("Manutenção do órgão", ["manutenção", "órgão"], id="pt"),
        ],
    )
    def test_a_latin_query_with_diacritics_is_not_fragmented(self, query: str, expected: list[str]) -> None:
        assert extract_query_entities(query) == expected

    @pytest.mark.parametrize(
        "query",
        ["엘리베이터 제조사", "安装位置は？", "設置場所を教えて", "Расположение установки"],
    )
    def test_a_non_latin_query_yields_tokens(self, query: str) -> None:
        assert extract_query_entities(query) != []

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            pytest.param("空調機の型番", ["空調機", "型番"], id="han-hiragana-han"),
            pytest.param(
                "エレベーターの設置場所",
                ["エレベーター", "設置場所"],
                id="katakana-hiragana-han",
            ),
            pytest.param("メーカーと型番を教えて", ["メーカー", "型番"], id="mixed-drops-hiragana-grammar"),
        ],
    )
    def test_a_space_free_query_splits_at_script_transitions_without_arbitrary_bigrams(
        self, query: str, expected: list[str]
    ) -> None:
        assert extract_query_entities(query) == expected

    def test_pure_han_clause_stays_one_reverse_containment_candidate(self) -> None:
        plan = build_query_search_plan("空調設備型番")

        assert plan.terms == ("空調設備型番",)
        assert plan.containers == ("空調設備型番",)

    def test_supplementary_han_is_not_treated_as_generic_text(self) -> None:
        assert extract_query_entities("𠮷野家") == ["𠮷野家"]

    def test_hiragana_vocabulary_is_reachable_through_the_container(self) -> None:
        # Hiragana-only runs are grammar in mixed Japanese text, so they are not
        # forward terms; the container keeps genuine hiragana words reachable.
        plan = build_query_search_plan("りんご")
        assert plan.terms == ()
        assert plan.containers == ("りんご",)

        plan = build_query_search_plan("りんごの価格")
        assert plan.terms == ("価格",)
        assert "りんご" in plan.containers[0]

    def test_standalone_one_grapheme_particles_are_dropped_by_the_dense_floor(self) -> None:
        assert extract_query_entities("の は を") == []

    def test_an_identifier_does_not_absorb_the_particle_after_it(self) -> None:
        assert extract_query_entities("MSZ-ZW252の仕様") == ["msz-zw252", "仕様"]

    def test_two_character_terms_survive_in_a_dense_script(self) -> None:
        assert "型番" in extract_query_entities("型番は何ですか")

    @pytest.mark.parametrize(
        "word",
        [
            pytest.param("เครื่องปรับอากาศ", id="thai"),
            pytest.param("कर्मचारी", id="devanagari"),
            pytest.param("কর্মচারী", id="bengali"),
        ],
    )
    def test_combining_script_word_remains_one_token(self, word: str) -> None:
        assert extract_query_entities(word) == [word]

    @pytest.mark.parametrize(
        "word",
        [
            pytest.param("รุ่น", id="thai-model"),
            pytest.param("कक्षा", id="devanagari-class"),
            pytest.param("শ্রেণী", id="bengali-class"),
        ],
    )
    def test_two_grapheme_non_latin_word_survives_without_weakening_latin_floor(self, word: str) -> None:
        assert extract_query_entities(word) == [word]
        assert extract_query_entities("go db") == []

    def test_no_space_thai_question_uses_the_complete_run_as_a_container(self) -> None:
        plan = build_query_search_plan("เครื่องปรับอากาศรุ่นอะไร")

        assert plan.terms == ("เครื่องปรับอากาศรุ่นอะไร",)
        assert plan.containers == plan.terms
        assert "เครื่องปรับอากาศ" in plan.containers[0]
        assert "รุ่น" in plan.containers[0]

    def test_a_long_no_space_thai_question_still_yields_a_container(self) -> None:
        # The per-term cost bound must not discard the run a no-space script needs:
        # 65 graphemes / 85 code points is an ordinary Thai question length.
        query = "ขอทราบรายละเอียดของเครื่องปรับอากาศทั้งหมดในอาคารนี้รวมถึงผู้ผลิตรุ่นและวันที่ติดตั้ง"
        assert len(query) > MAX_SEARCH_TOKEN_CODEPOINTS

        plan = build_query_search_plan(query, max_count=5)

        assert plan.containers == (query,)
        assert "เครื่องปรับอากาศ" in plan.containers[0]
        assert "ผู้ผลิต" in plan.containers[0]

    def test_indic_function_words_cannot_hide_the_requested_concepts(self) -> None:
        # इस / और pass the two-grapheme floor and are not English stop words, so
        # they can still occupy forward slots; the per-word reverse regions are what
        # guarantee the nouns stay reachable at the production budget.
        plan = build_query_search_plan("इस भवन में कक्षा और कर्मचारी की जानकारी दिखाएं", max_count=5)

        assert len(plan.terms) == 5
        assert "कर्मचारी" in plan.containers
        assert "भवन" in plan.containers

    def test_korean_particle_suffixes_do_not_hide_the_stored_label(self) -> None:
        # Korean spaces phrases but agglutinates particles onto the noun, so the
        # forward term is 제조사와 while the stored label is 제조사 — found inside
        # that word by the reverse direction.
        plan = build_query_search_plan("이 시설에 설치된 엘리베이터의 제조사와 모델명을 알려줘", max_count=5)

        assert "제조사와" in plan.terms
        assert "제조사와" in plan.containers
        assert any("엘리베이터" in container for container in plan.containers)

    def test_a_latin_query_gets_no_whole_query_container(self) -> None:
        # Latin prose has reliable whitespace boundaries, so the forward direction
        # already covers it and whole-query reverse matching would only add loose
        # matches on very short labels.
        assert build_query_search_plan("show me the elevator model").containers == ()

    def test_a_mixed_query_keeps_latin_words_out_of_the_container(self) -> None:
        # Otherwise a two-letter Latin label would match ordinary English words and
        # STOP_WORDS would be silently suspended for any query holding one
        # non-Latin letter.
        plan = build_query_search_plan("What is the 空調 rate and the order status?", max_count=5)

        assert plan.containers == ("空調",)
        for latin_word in ("what", "the", "and", "rate", "order", "status"):
            assert latin_word not in plan.containers[0].lower()

    def test_container_regions_have_their_own_bound(self) -> None:
        # Regions must not share the forward budget: for a spaced non-Latin script
        # one region IS one word, and a five-slot cap evicted the requested noun
        # from the reverse direction too.
        from coa_serve.query_utils import MAX_CONTAINER_REGIONS

        plan = build_query_search_plan("空調、設備、型番、価格、納期、出荷", max_count=3)

        assert len(plan.terms) == 3
        assert plan.containers == ("空調", "設備", "型番", "価格", "納期", "出荷")

        many = "、".join(f"部{chr(0x4E00 + index)}" for index in range(MAX_CONTAINER_REGIONS + 5))
        assert len(build_query_search_plan(many, max_count=3).containers) == MAX_CONTAINER_REGIONS

    def test_a_container_region_stops_at_digits_and_punctuation(self) -> None:
        # A region holds script text only, so a numeric model-number fragment can
        # never become a reverse haystack.
        assert build_query_search_plan("MSZ-ZW252の仕様").containers == ("の仕様",)
        assert build_query_search_plan("空調設備、型番").containers == ("空調設備", "型番")

    def test_single_character_regions_are_dropped(self) -> None:
        # Both sinks require a stored label of at least two characters, so a
        # one-character region could never match and would only spend a slot.
        assert build_query_search_plan("空 の 設 備").containers == ()

    def test_dotted_capital_i_lowering_does_not_split_istanbul(self) -> None:
        assert extract_query_entities("İstanbul") == ["i\u0307stanbul"]

    def test_minimum_length_counts_graphemes_not_code_points(self) -> None:
        assert len("a\u0301b") == 3
        assert extract_query_entities("a\u0301b") == []

    def test_orphan_combining_marks_are_ignored(self) -> None:
        assert extract_query_entities("\u0301\u0308 claim") == ["claim"]

    def test_nonsemantic_runs_do_not_consume_query_budget(self) -> None:
        assert extract_query_entities("___ --- ーー 😺") == []
        assert extract_query_entities("___ alpha --- beta", max_count=2) == ["alpha", "beta"]

    def test_overlength_run_does_not_reach_any_keyword_consumer(self) -> None:
        assert extract_query_entities("x" * 65) == []
        assert extract_query_entities(f"{'x' * 65} policy", max_count=1) == ["policy"]

    def test_numeric_and_model_identifiers_remain_searchable(self) -> None:
        assert extract_query_entities("X12 2026") == ["x12", "2026"]

    def test_english_behaviour_is_unchanged(self) -> None:
        assert extract_query_entities("show me the order-id for S3") == ["order-id"]
        assert extract_query_entities("What is the elevator model?") == ["elevator", "model"]

    def test_five_term_budget_keeps_requested_japanese_concepts_not_hiragana_grammar(self) -> None:
        plan = build_query_search_plan(
            "この施設に設置されているエレベーターのメーカーと型番を教えて",
            max_count=5,
        )

        assert plan.terms == ("施設", "設置", "エレベーター", "メーカー", "型番")
        assert plan.containers[0].endswith("型番を教えて")

    def test_han_sentence_keeps_suffix_concepts_in_one_bounded_container(self) -> None:
        plan = build_query_search_plan("请告诉我这台空调设备的制造商和型号", max_count=5)

        assert len(plan.terms) == 1
        assert "空调设备" in plan.containers[0]
        assert "制造商" in plan.containers[0]
        assert "型号" in plan.containers[0]

    def test_decomposed_accent_is_preserved_without_query_only_normalization(self) -> None:
        composed = "café"
        decomposed = unicodedata.normalize("NFD", composed)

        assert extract_query_entities(composed) == [composed]
        assert extract_query_entities(decomposed) == [decomposed]
        assert composed != decomposed

    def test_halfwidth_and_fullwidth_katakana_preserve_the_stored_spelling(self) -> None:
        halfwidth = "ｴﾚﾍﾞｰﾀｰ"
        fullwidth = "エレベーター"

        assert extract_query_entities(halfwidth) == [halfwidth]
        assert extract_query_entities(fullwidth) == [fullwidth]
        assert halfwidth != fullwidth

    def test_eszett_is_lowered_the_way_lcase_lowers_it(self) -> None:
        tokens = extract_query_entities("Straße Wartung")

        assert tokens == ["straße", "wartung"]
        assert tokens[0] in "Hauptstraße Berlin".lower()

    def test_greek_final_sigma_survives_a_lowercase_query(self) -> None:
        tokens = extract_query_entities("τέλος διαδικασίας")

        assert "τέλος" in tokens
        assert "τέλοσ" not in tokens

    def test_a_repeated_term_does_not_spend_the_budget_twice(self) -> None:
        tokens = extract_query_entities("空調機と空調機の型番")

        assert tokens == ["空調機", "型番"]

    def test_first_occurrence_order_is_preserved(self) -> None:
        assert extract_query_entities("elevator model elevator width") == ["elevator", "model", "width"]

    def test_over_budget_candidates_are_spread_across_the_full_query(self) -> None:
        assert extract_query_entities("one two three four five six seven", max_count=5) == [
            "one",
            "three",
            "four",
            "six",
            "seven",
        ]

    def test_maximum_query_length_is_accepted(self) -> None:
        assert extract_query_entities("x" * MAX_QUERY_CODEPOINTS) == []

    def test_oversized_query_is_rejected_before_grapheme_scanning(self) -> None:
        with (
            patch("coa_serve.query_utils._GRAPHEME_RE") as grapheme_re,
            pytest.raises(ValueError, match=str(MAX_QUERY_CODEPOINTS)),
        ):
            extract_query_entities("🇯🇵" * (MAX_QUERY_CODEPOINTS // 2 + 1))

        grapheme_re.finditer.assert_not_called()

    def test_an_empty_or_blank_query_yields_nothing(self) -> None:
        assert extract_query_entities("") == []
        assert extract_query_entities("   \n\t ") == []


@pytest.mark.unit
class TestContainerRegionScriptCoverage:
    """Which scripts get a reverse container, and whether a word survives whole.

    A region must keep a word intact — combining marks and the in-word joiners
    included — or reverse containment searches a haystack the label cannot be found
    in. Scripts whose inflection is suffix substitution get no container at all,
    because containment cannot recover a label the forward term missed there.
    """

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            pytest.param("เครื่องปรับอากาศรุ่นอะไร", ("เครื่องปรับอากาศรุ่นอะไร",), id="thai-marks-attached"),
            pytest.param("ຂໍ້ມູນ", ("ຂໍ້ມູນ",), id="lao"),
            pytest.param("ព័ត៌មាន", ("ព័ត៌មាន",), id="khmer"),
            pytest.param("कर्मचारी संख्या", ("कर्मचारी", "संख्या"), id="devanagari-conjuncts"),
            pytest.param("কর্মচারীর ভবনের", ("কর্মচারীর", "ভবনের"), id="bengali-conjuncts"),
            pytest.param("الكتاب العربي", ("الكتاب", "العربي"), id="arabic"),
            pytest.param("טקסט עברי", ("טקסט", "עברי"), id="hebrew"),
            pytest.param("제조사와 모델명을", ("제조사와", "모델명을"), id="hangul-per-word"),
            pytest.param("空調設備の型番", ("空調設備の型番",), id="han-kana-one-run"),
            pytest.param("ру́сский язы́к", ("ру́сский", "язы́к"), id="cyrillic-decomposed"),
        ],
    )
    def test_a_word_survives_whole_in_a_container_script(self, query: str, expected: tuple[str, ...]) -> None:
        assert build_query_search_plan(query).containers == expected

    def test_in_word_joiners_do_not_split_a_container(self) -> None:
        # ZWNJ is a format character but sits INSIDE a Persian compound, and the
        # forward terms keep it, so the container must keep it too.
        assert build_query_search_plan("کتاب\u200cها").containers == ("کتاب\u200cها",)

    def test_a_decomposed_word_is_not_split_by_its_combining_marks(self) -> None:
        # 46 BMP combining marks carry scx=Latin, so subtracting Latin from the mark
        # class would cut a decomposed word in half and drop the mark.
        composed = "कक्षा"
        decomposed = unicodedata.normalize("NFD", composed)

        assert build_query_search_plan(decomposed).containers == (decomposed,)

    @pytest.mark.parametrize(
        "query",
        [
            pytest.param("κείμενο διαδικασίας", id="greek"),
            pytest.param("show me the elevator model", id="latin"),
            pytest.param(unicodedata.normalize("NFD", "café"), id="latin-decomposed"),
        ],
    )
    def test_a_stem_rewriting_spaced_script_gets_no_container(self, query: str) -> None:
        # Greek inflection rewrites the ending rather than appending to the citation
        # form (``νόμος`` is not inside ``νόμου``), so a container adds no recall —
        # only a path for a short label to match inside a longer unrelated word.
        # Forward terms still cover these scripts.
        plan = build_query_search_plan(query)

        assert plan.containers == ()
        assert plan.terms != ()

    def test_cyrillic_case_forms_keep_the_stored_label_reachable(self) -> None:
        # A Russian case form appends to the nominative stem, so the stored label is
        # inside the query word — recall the forward direction reaches only when the
        # query happens to use the nominative.
        plan = build_query_search_plan("расположение установки в столе", max_count=5)

        assert "столе" in plan.containers
        assert "установки" in plan.containers
        for stored_label in ("стол", "установк"):
            assert any(stored_label in container for container in plan.containers)

    def test_a_default_ignorable_inside_a_word_does_not_defeat_containment(self) -> None:
        # An ideographic variation selector is category Mn, so it survives a word run
        # and would leave a stored ``渡邊`` outside an IME-produced ``渡󠄀邊``.
        plan = build_query_search_plan("渡\U000e0100邊の型番")

        assert "渡邊" in plan.terms
        assert plan.containers == ("渡邊の型番",)
        assert "\U000e0100" not in plan.containers[0]

    @pytest.mark.parametrize(
        "invisible",
        [
            pytest.param("\u200b", id="zero-width-space"),
            pytest.param("\u00ad", id="soft-hyphen"),
            pytest.param("\u2060", id="word-joiner"),
            pytest.param("\ufeff", id="zero-width-no-break-space"),
            pytest.param("\u200e", id="left-to-right-mark"),
            pytest.param("\U000e0100", id="variation-selector"),
        ],
    )
    def test_an_invisible_character_inside_a_word_is_removed_not_made_a_boundary(self, invisible: str) -> None:
        # A format-category default-ignorable falls outside the region class, so
        # unless it is stripped FIRST it splits the word and costs both directions
        # their match.
        plan = build_query_search_plan(f"型{invisible}番を教えて")

        assert plan.terms == ("型番",)
        assert plan.containers == ("型番を教えて",)

    def test_stripping_cannot_leave_a_candidate_below_the_length_floors(self) -> None:
        # U+3164 HANGUL FILLER is a default-ignorable LETTER, so stripping after the
        # grapheme floor would have emitted a one-character needle.
        assert build_query_search_plan("\u3164가") == QuerySearchPlan(terms=(), containers=())
        assert build_query_search_plan("\u200b\u00ad") == QuerySearchPlan(terms=(), containers=())

    def test_meaningful_joiners_survive_the_strip(self) -> None:
        # ZWNJ and ZWJ are default-ignorable but carry meaning inside a word.
        assert build_query_search_plan("کتاب\u200cها").containers == ("کتاب\u200cها",)

    def test_abutting_excluded_script_bounds_the_region(self) -> None:
        assert build_query_search_plan("空調κείμενο").containers == ("空調",)


@pytest.mark.unit
class TestSearchPlanObservability:
    """A dropped candidate must be visible in logs, not only in behaviour.

    A no-space script has no working forward arm to fall back on — for pure Chinese
    the forward term is the whole clause — so a silently dropped region is a
    silently dropped chance to match anything.
    """

    def test_region_truncation_is_logged_with_the_counts(self) -> None:
        from coa_serve.query_utils import MAX_CONTAINER_REGIONS

        query = "、".join(f"部{chr(0x4E00 + index)}" for index in range(MAX_CONTAINER_REGIONS + 4))

        with patch("coa_serve.query_utils.logger") as log:
            plan = build_query_search_plan(query, max_count=5)

        assert len(plan.containers) == MAX_CONTAINER_REGIONS
        log.info.assert_called_once()
        event, kwargs = log.info.call_args[0][0], log.info.call_args[1]
        assert event == "query_container_regions_truncated"
        assert kwargs["regions"] == MAX_CONTAINER_REGIONS + 4
        assert kwargs["limit"] == MAX_CONTAINER_REGIONS

    def test_no_log_when_every_region_survives(self) -> None:
        with patch("coa_serve.query_utils.logger") as log:
            build_query_search_plan("空調設備、型番", max_count=5)

        log.info.assert_not_called()


@pytest.mark.unit
class TestNamespaceGraphPrefix:
    def test_prefix_always_ends_with_a_delimiter(self):
        # The trailing "/" is what makes STRSTARTS scoping exact enough: a namespace
        # cannot contain "/", so "…/ns-a/" can never prefix-match "…/ns-ab/".
        assert namespace_graph_prefix("https://g.local/{namespace}", "ns-a") == "https://g.local/ns-a/"
        assert namespace_graph_prefix("https://g.local/{namespace}/", "ns-a") == "https://g.local/ns-a/"

    def test_a_sibling_namespace_is_not_a_prefix_of_another(self):
        short = namespace_graph_prefix("https://g.local/{namespace}", "ns-a")
        longer = namespace_graph_prefix("https://g.local/{namespace}", "ns-ab")

        assert not longer.startswith(short)


@pytest.mark.unit
class TestGraphScopedBody:
    """How the graphs are NAMED, not just which ones — see ``graph_scoped_body``.

    A ``VALUES ?g { <a> <b> }`` binding and a ``GRAPH <a>``/``GRAPH <b>`` UNION
    select the same quads, so no result-level test can tell them apart. They are
    not interchangeable to the planner: the constant form is pushed into the index
    scan, the VALUES form is a materialised sequence joined afterwards. Measured
    end to end on one deployed build, an idle cluster, parallelism 1: the
    one-graph namespace (where a single VALUES row folds to a constant anyway) ran
    the join-path query in 10ms, while the two-graph namespace ReadTimeout at
    16.5s on three separate queries — two of which carry no binding other than the
    graph. Hence these assertions are about query TEXT.
    """

    _PREFIX = "https://g.local/ns/"
    _A = "https://g.local/ns/induced"
    _B = "https://g.local/ns/curated"

    def test_one_graph_is_named_as_a_constant(self):
        sparql = graph_scoped_body("?s ?p ?o .", self._PREFIX, [self._A])

        assert f"GRAPH <{self._A}> {{" in sparql
        assert "VALUES ?g" not in sparql
        assert "STRSTARTS" not in sparql

    def test_several_graphs_become_a_union_of_constants_not_a_values_join(self):
        sparql = graph_scoped_body("?s ?p ?o .", self._PREFIX, [self._A, self._B])

        assert f"GRAPH <{self._A}> {{" in sparql
        assert f"GRAPH <{self._B}> {{" in sparql
        assert "UNION" in sparql
        assert "VALUES ?g" not in sparql
        assert "STRSTARTS" not in sparql

    def test_the_union_branches_are_group_graph_patterns(self):
        """``A UNION B`` is a syntax error unless both operands are braced."""
        sparql = graph_scoped_body("?s ?p ?o .", self._PREFIX, [self._A, self._B])

        left, _, right = sparql.partition("UNION")
        for side in (left, right):
            assert side.count("{") == side.count("}"), side

    def test_an_implausible_graph_count_degrades_to_the_prefix_filter(self):
        """Inlining repeats the body per graph, and a body may carry 175 IRIs.

        Namespaces publish one graph per ontology, so this bound is a guard, and
        the fallback is the form main shipped: slow, never broken.
        """
        many = [f"{self._PREFIX}g{i}" for i in range(_MAX_INLINED_GRAPHS + 1)]

        sparql = graph_scoped_body("?s ?p ?o .", self._PREFIX, many)

        assert f'FILTER(STRSTARTS(STR(?g), "{self._PREFIX}"))' in sparql
        assert "GRAPH ?g" in sparql
        assert f"GRAPH <{many[0]}>" not in sparql

    def test_the_implausible_graph_count_is_the_one_fallback_that_warns(self):
        """Resolution SUCCEEDED and we still fell back — the invisible case.

        The resolver's own cap (tbox_context._MAX_GRAPHS, 50) is well above this
        one, so a 9..50-graph namespace pays the cluster-wide scan while every
        other log line says its graphs resolved fine. That is exactly the shape of
        silent degradation this change exists to remove, so it warns.
        """
        many = [f"{self._PREFIX}g{i}" for i in range(_MAX_INLINED_GRAPHS + 1)]

        with patch("coa_serve.query_utils.logger") as log:
            graph_scoped_body("?s ?p ?o .", self._PREFIX, many)

        log.warning.assert_called_once()
        event, kwargs = log.warning.call_args[0][0], log.warning.call_args[1]
        assert event == "graph_scope_not_inlined"
        assert kwargs["graphs"] == _MAX_INLINED_GRAPHS + 1
        assert kwargs["max_inlined"] == _MAX_INLINED_GRAPHS

    def test_an_unsafe_iri_is_dropped_rather_than_interpolated(self):
        """The guard sits at the interpolation point, not only at resolution.

        Resolvers apply the same test, but ``graph_iris`` also reaches here through
        a plain dataclass field (``TBoxContext.graph_iris``) and through
        ``_retry_vkg``'s ``Any``-typed reuse parameter, neither of which
        re-validates. A closing angle bracket would otherwise escape the
        ``GRAPH <...>`` term.
        """
        hostile = f"{self._PREFIX}g> }} ?s ?p ?o }} #"

        with patch("coa_serve.query_utils.logger") as log:
            sparql = graph_scoped_body("?s ?p ?o .", self._PREFIX, [self._A, hostile])

        assert f"GRAPH <{self._A}>" in sparql
        assert hostile not in sparql
        assert "UNION" not in sparql, "one graph survived, so there is nothing to union"
        assert log.warning.call_args[0][0] == "graph_scope_unsafe_iri_dropped"

    def test_dropping_every_iri_degrades_to_the_prefix_filter(self):
        with patch("coa_serve.query_utils.logger"):
            sparql = graph_scoped_body("?s ?p ?o .", self._PREFIX, ["not an iri"])

        assert f'FILTER(STRSTARTS(STR(?g), "{self._PREFIX}"))' in sparql

    def test_unresolved_graphs_degrade_to_the_prefix_filter(self):
        for unresolved in ([], None):
            with patch("coa_serve.query_utils.logger") as log:
                sparql = graph_scoped_body("?s ?p ?o .", self._PREFIX, unresolved)

            assert f'FILTER(STRSTARTS(STR(?g), "{self._PREFIX}"))' in sparql
            assert "GRAPH ?g" in sparql
            # Silent here on purpose: the caller that failed to resolve has
            # already logged tbox_graph_resolve_failed with the exception, so a
            # second line per query would be noise, not signal.
            log.warning.assert_not_called()


@pytest.mark.unit
class TestObjectPropertiesSparql:
    """One FK-edge query shared by the T-Box builder and the traversal tool."""

    def test_selects_the_edge_shape_both_callers_parse(self):
        sparql = object_properties_sparql("https://g.local/ns/", limit=200)

        for var in ("?op", "?opLabel", "?domain", "?domainLabel", "?range", "?rangeLabel"):
            assert var in sparql
        assert 'FILTER(STRSTARTS(STR(?g), "https://g.local/ns/"))' in sparql
        assert "LIMIT 200" in sparql

    def test_the_mapped_gate_is_opt_in_and_covers_both_ends(self):
        ungated = object_properties_sparql("https://g.local/ns/", limit=10)
        assert "isMapped" not in ungated

        gated = object_properties_sparql("https://g.local/ns/", limit=10, mapped_gate_iri="urn:coa:vocab#isMapped")
        assert "?domain <urn:coa:vocab#isMapped> true ." in gated
        assert "?range <urn:coa:vocab#isMapped> true ." in gated

    def test_comment_is_opt_in(self):
        assert "?comment" not in object_properties_sparql("https://g.local/ns/", limit=10)
        assert "?comment" in object_properties_sparql("https://g.local/ns/", limit=10, with_comment=True)

    def test_domain_and_range_types_are_not_re_asserted(self):
        # Deliberate: those checks are redundant and pushed the query past its
        # Neptune timeout on a 400-table namespace.
        sparql = object_properties_sparql("https://g.local/ns/", limit=10)

        assert "?domain a owl:Class" not in sparql
        assert "?range a owl:Class" not in sparql

    def test_domain_anchor_is_opt_in_and_precedes_the_edge_scan(self):
        """Graph scoping alone left this query timing out at 16.5s on the
        412-class namespace, because ``?domain rdfs:label ?domainLabel`` with
        ``?domain`` unbound is a scan of every label in the graph. Anchoring
        ``?domain`` is what makes the label joins per-edge."""
        anchored = object_properties_sparql(
            "https://g.local/ns/",
            limit=10,
            domain_iris=["https://g.local/o#A", "https://g.local/o#B"],
        )

        assert "VALUES ?domain { <https://g.local/o#A> <https://g.local/o#B> }" in anchored
        # Before the edge scan, or the planner still starts from every ObjectProperty.
        assert anchored.index("VALUES ?domain") < anchored.index("?op a owl:ObjectProperty")

    def test_no_domain_anchor_keeps_the_whole_fk_graph(self):
        """The traversal tool walks edges the prompt never mentions, so it must
        keep passing nothing here and get every edge."""
        for empty in (None, []):
            sparql = object_properties_sparql("https://g.local/ns/", limit=10, domain_iris=empty)
            assert "VALUES ?domain" not in sparql
