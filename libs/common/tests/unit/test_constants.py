# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for shared constants — to_graphrag_tenant_id() and validation helpers."""

import importlib
import re

import pytest
from coa_common.constants import (
    bucket_grants_namespace,
    bucket_namespace_tag_key,
    canonical_col,
    datasource_external_id,
    graphrag_chunk_index_name,
    graphrag_index_names,
    namespace_tag_condition_patterns,
    namespace_tag_key,
    ontology_artifact_s3_key,
    ontology_vector_index_name,
    parse_namespace_tag,
    split_sql_ident_path,
    sql_ident,
    sql_qualified_table,
    to_graphrag_tenant_id,
    validate_id,
    validate_namespace_id,
    validate_namespace_name,
    validate_s3_prefix,
)

pytestmark = pytest.mark.unit


class TestToGraphragTenantId:
    """Verify the namespace_id → TenantId mapping.

    Strips hyphens from the UUID v4 namespace_id and truncates to 25 chars,
    satisfying the GraphRAG TenantId constraint (1–25 lowercase alphanumeric).
    All doc sources within a namespace share the same tenant ID.
    """

    def _expected(self, namespace_id: str) -> str:
        return namespace_id.replace("-", "")[:25]

    def test_basic_uuid(self):
        ns = "550e8400-e29b-41d4-a716-446655440000"
        result = to_graphrag_tenant_id(ns)
        assert result == "550e8400e29b41d4a71644665"

    def test_hyphens_stripped(self):
        ns = "550e8400-e29b-41d4-a716-446655440000"
        result = to_graphrag_tenant_id(ns)
        assert "-" not in result

    def test_always_25_chars_for_uuid(self):
        ns = "550e8400-e29b-41d4-a716-446655440000"
        assert len(to_graphrag_tenant_id(ns)) == 25

    def test_deterministic(self):
        """Same namespace always produces the same tenant ID."""
        ns = "550e8400-e29b-41d4-a716-446655440000"
        assert to_graphrag_tenant_id(ns) == to_graphrag_tenant_id(ns)

    def test_different_namespaces_different_tenant_ids(self):
        """Different namespaces produce different tenant IDs."""
        a = to_graphrag_tenant_id("550e8400-e29b-41d4-a716-446655440000")
        b = to_graphrag_tenant_id("f47ac10b-58cc-4372-a567-0e02b2c3d479")
        assert a != b

    def test_doc_source_id_ignored(self):
        """doc_source_id parameter is accepted but ignored — same namespace always
        produces the same tenant ID regardless of doc_source_id."""
        ns = "550e8400-e29b-41d4-a716-446655440000"
        assert to_graphrag_tenant_id(ns, "ds-1") == to_graphrag_tenant_id(ns, "ds-2")
        assert to_graphrag_tenant_id(ns, "ds-1") == to_graphrag_tenant_id(ns)

    def test_backward_compatible_two_arg_call(self):
        """Existing call sites passing doc_source_id still work."""
        ns = "550e8400-e29b-41d4-a716-446655440000"
        ds = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        result = to_graphrag_tenant_id(ns, ds)
        assert result == self._expected(ns)
        assert len(result) == 25

    def test_valid_graphrag_tenant_id_format(self):
        """Result satisfies GraphRAG TenantId constraint: 1–25 lowercase alphanumeric."""
        ns = "550e8400-e29b-41d4-a716-446655440000"
        result = to_graphrag_tenant_id(ns)
        assert 1 <= len(result) <= 25
        assert re.fullmatch(r"[0-9a-f]{25}", result), f"Invalid tenant_id: {result}"

    @pytest.mark.parametrize(
        "ns",
        [
            "550e8400-e29b-41d4-a716-446655440000",
            "f47ac10b-58cc-4372-a567-0e02b2c3d479",
            "00000000-0000-4000-8000-000000000000",
            "ffffffff-ffff-4fff-bfff-ffffffffffff",
        ],
    )
    def test_always_valid_for_uuid_v4(self, ns: str):
        result = to_graphrag_tenant_id(ns)
        assert len(result) == 25
        assert re.fullmatch(r"[0-9a-f]{25}", result)


class TestValidateId:
    @pytest.mark.parametrize("value", ["abc", "abc-123", "a_b", "ABC_123-x"])
    def test_valid_ids_pass(self, value: str):
        validate_id(value, "myId")  # should not raise

    @pytest.mark.parametrize("value", ["", "has space", "bad/slash", "dot.dot", "semi;colon"])
    def test_invalid_ids_raise(self, value: str):
        with pytest.raises(ValueError, match="Invalid myId"):
            validate_id(value, "myId")


class TestValidateNamespaceId:
    def test_valid_uuid_v4_passes(self):
        validate_namespace_id("550e8400-e29b-41d4-a716-446655440000")

    @pytest.mark.parametrize("value", ["", "not-a-uuid", "550e8400e29b41d4a716446655440000"])
    def test_invalid_raises(self, value: str):
        with pytest.raises(ValueError, match="Must be a UUID v4"):
            validate_namespace_id(value)


class TestValidateNamespaceName:
    @pytest.mark.parametrize("value", ["bird", "bird-benchmark", "a", "A1_b-2"])
    def test_valid_names_pass(self, value: str):
        validate_namespace_name(value)

    @pytest.mark.parametrize("value", ["", "-leading-hyphen", "_underscore", "has space", "x" * 129])
    def test_invalid_names_raise(self, value: str):
        with pytest.raises(ValueError, match="Invalid namespace"):
            validate_namespace_name(value)


class TestValidateS3Prefix:
    def test_empty_string_allowed(self):
        validate_s3_prefix("", "prefix")  # no raise

    @pytest.mark.parametrize("value", ["healthcare/", "documents/2024/", "a-b_c.d/"])
    def test_valid_relative_prefixes_pass(self, value: str):
        validate_s3_prefix(value, "prefix")

    @pytest.mark.parametrize("value", ["/leading", "../traversal", "s3://bucket/x", "has space"])
    def test_unsafe_prefixes_raise(self, value: str):
        with pytest.raises(ValueError, match="Invalid prefix"):
            validate_s3_prefix(value, "prefix")


class TestCanonicalCol:
    def test_lowercases_and_replaces_spaces(self):
        assert canonical_col("First Name") == "first_name"

    def test_strips_illegal_characters(self):
        assert canonical_col("aCL IgG (%)") == "acl_igg_"

    def test_digit_leading_gets_underscore_prefix(self):
        assert canonical_col("1st") == "_1st"

    def test_empty_result_defaults_to_col(self):
        assert canonical_col("()%") == "col"


class TestSqlIdent:
    def test_wraps_in_double_quotes(self):
        assert sql_ident("First Name") == '"First Name"'

    def test_escapes_embedded_double_quotes(self):
        assert sql_ident('a"b') == '"a""b"'

    def test_none_becomes_empty_quoted(self):
        assert sql_ident(None) == '""'  # type: ignore[arg-type]


class TestSqlQualifiedTable:
    """#149 A: the shared helper both the H2 DDL and the R2RML writer depend on
    for byte-identical qualified table identifiers."""

    def test_qualifies_with_schema(self):
        assert sql_qualified_table("orders", "sales") == '"sales"."orders"'

    def test_bare_when_no_schema(self):
        assert sql_qualified_table("orders") == '"orders"'
        assert sql_qualified_table("orders", None) == '"orders"'

    def test_empty_schema_is_treated_as_bare(self):
        # Falsy schema must not produce a leading-dot ("".orders) key.
        assert sql_qualified_table("orders", "") == '"orders"'

    def test_escapes_embedded_quotes_in_both_parts(self):
        assert sql_qualified_table('a"b', 's"c') == '"s""c"."a""b"'

    def test_preserves_special_characters(self):
        assert sql_qualified_table("events daily", "raw-zone") == '"raw-zone"."events daily"'


class TestSplitSqlIdentPath:
    """The inverse of :func:`sql_ident` per dotted segment.

    A textual ``.rsplit(".")`` is the tempting alternative and it is wrong on
    exactly the names that matter: it turns the single identifier ``"q1.results"``
    into two unbalanced halves, which in generated DDL is a whole-file syntax
    error rather than one bad table.
    """

    def test_qualified_name_splits_into_segments(self):
        assert split_sql_ident_path('"public"."customers"') == ["public", "customers"]

    def test_dot_inside_one_identifier_is_not_a_separator(self):
        assert split_sql_ident_path('"q1.results"') == ["q1.results"]

    def test_doubled_quotes_are_one_literal_quote(self):
        assert split_sql_ident_path('"a""b"') == ['a"b']

    def test_unquoted_name_still_splits(self):
        assert split_sql_ident_path("public.customers") == ["public", "customers"]

    @pytest.mark.parametrize(
        "segments",
        [
            ["customers"],
            ["public", "customers"],
            ["q1.results"],
            ['a"b', "c.d"],
            ["public__2f77729f", "customers"],
        ],
    )
    def test_round_trips_through_sql_ident(self, segments):
        literal = ".".join(sql_ident(s) for s in segments)
        assert split_sql_ident_path(literal) == segments


class TestIndexNameHelpers:
    def test_graphrag_chunk_index_name(self):
        ns = "550e8400-e29b-41d4-a716-446655440000"
        assert graphrag_chunk_index_name(ns) == "chunk_550e8400e29b41d4a71644665"

    def test_ontology_vector_index_name_default_namespace_returns_bare_prefix(self):
        assert ontology_vector_index_name("coa-vectors", "default") == "coa-vectors"

    def test_ontology_vector_index_name_appends_namespace(self):
        assert ontology_vector_index_name("coa-vectors", "ns-1") == "coa-vectors-ns-1"


class TestOntologyArtifactS3Key:
    def test_builds_key(self):
        key = ontology_artifact_s3_key("pc-insurance", "latest", "examples.json")
        assert key == "ontologies/pc-insurance/latest/examples.json"

    def test_path_traversal_filename_raises(self):
        with pytest.raises(ValueError):
            ontology_artifact_s3_key("pc-insurance", "latest", "../secret")

    def test_invalid_namespace_raises(self):
        with pytest.raises(ValueError, match="Invalid namespace"):
            ontology_artifact_s3_key("bad namespace", "latest", "examples.json")


class TestBrandTokensIndependentOfResourcePrefix:
    """Data-plane identifiers must not track RESOURCE_PREFIX.

    CDK injects ``RESOURCE_PREFIX`` as ``{prefix}-{env}-`` (e.g. ``scl-dev-``)
    because its consumers build resource names from it. These tokens used to
    derive from it, so a deployment with ``resource_prefix=scl`` wrote IRIs under
    ``http://scl-dev.amazon.com/vocab/scl-dev#`` while readers used their own
    compiled-in default and silently matched nothing.
    """

    def test_graph_tokens_ignore_resource_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RESOURCE_PREFIX", "scl-dev-")
        for var in ("BRAND", "GRAPH_BASE_URI", "URN_PREFIX", "VOCAB_PREFIX", "VOCAB_URI"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("DZ_TYPE_PREFIX", raising=False)
        monkeypatch.delenv("EVENT_SOURCE_PREFIX", raising=False)

        mod = importlib.reload(importlib.import_module("coa_common.constants"))
        try:
            assert mod.RESOURCE_PREFIX == "scl-dev"
            assert mod.BRAND == "coa"
            assert mod.GRAPH_BASE_URI == "http://coa.amazon.com"
            assert mod.VOCAB_URI == "http://coa.amazon.com/vocab/coa#"
            assert mod.URN_PREFIX == "coa"
            assert mod.EVENT_SOURCE_PREFIX == "coa"
            assert mod.DZ_TYPE_PREFIX == "Coa"
        finally:
            monkeypatch.undo()
            importlib.reload(mod)

    def test_graph_base_uri_override_flows_into_vocab_uri(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GRAPH_BASE_URI", "http://example.internal")
        monkeypatch.delenv("VOCAB_URI", raising=False)

        mod = importlib.reload(importlib.import_module("coa_common.constants"))
        try:
            assert mod.VOCAB_URI == "http://example.internal/vocab/coa#"
        finally:
            monkeypatch.undo()
            importlib.reload(mod)


class TestGraphragIndexNames:
    """The full set of GraphRAG vector indexes a namespace can own.

    Namespace teardown deletes exactly these, so the list must stay in step with
    what the build path creates (``EMBEDDING_INDEXES`` in graph_build.py) plus
    any name that was ever created historically. A name missing here is an index
    that survives its namespace forever, and AOSS caps a collection at 1000.
    """

    def test_covers_current_and_legacy_prefixes(self):
        ns = "550e8400-e29b-41d4-a716-446655440000"
        tenant = to_graphrag_tenant_id(ns)
        assert graphrag_index_names(ns) == [
            f"chunk_{tenant}",
            f"topic_{tenant}",
            # Legacy: statements are no longer embedded, but namespaces ingested
            # before that change still have this index consuming quota.
            f"statement_{tenant}",
        ]

    def test_chunk_name_agrees_with_the_single_name_helper(self):
        """Two helpers derive the chunk index name; they must not drift."""
        ns = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        assert graphrag_chunk_index_name(ns) in graphrag_index_names(ns)

    def test_namespace_scoped_not_source_scoped(self):
        """All doc sources in a namespace share one index set — which is why
        namespace deletion owns these and source deletion does not."""
        ns = "550e8400-e29b-41d4-a716-446655440000"
        assert graphrag_index_names(ns) == graphrag_index_names(ns)

    def test_distinct_namespaces_get_distinct_indexes(self):
        a = graphrag_index_names("550e8400-e29b-41d4-a716-446655440000")
        b = graphrag_index_names("f47ac10b-58cc-4372-a567-0e02b2c3d479")
        assert not set(a) & set(b)


@pytest.mark.unit
class TestDatasourceExternalId:
    """The ExternalId presented when assuming a customer's cross-account role.

    The cross-account role ARN is caller-supplied, so this value — derived from
    the namespace, never from the request — is what binds an assume to the
    namespace entitled to it.
    """

    def test_derives_from_prefix_and_namespace(self, monkeypatch):
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev-")
        assert datasource_external_id("ns-1") == "coa-dev-ns-1"

    def test_distinct_namespaces_get_distinct_values(self, monkeypatch):
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev-")
        a = datasource_external_id("550e8400-e29b-41d4-a716-446655440000")
        b = datasource_external_id("f47ac10b-58cc-4372-a567-0e02b2c3d479")
        assert a != b

    def test_distinct_deployments_get_distinct_values(self, monkeypatch):
        """Two deployments must not present the same value for one namespace id."""
        ns = "550e8400-e29b-41d4-a716-446655440000"
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev-")
        dev = datasource_external_id(ns)
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-prod-")
        assert datasource_external_id(ns) != dev

    def test_reads_prefix_per_call_not_at_import(self, monkeypatch):
        """The value is read live so a redeploy under a new prefix takes effect."""
        monkeypatch.setenv("RESOURCE_PREFIX", "a-")
        assert datasource_external_id("ns") == "a-ns"
        monkeypatch.setenv("RESOURCE_PREFIX", "b-")
        assert datasource_external_id("ns") == "b-ns"


_NS_A = "550e8400-e29b-41d4-a716-446655440000"
_NS_B = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
_NS_C = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"


class TestNamespaceTagKey:
    """The resource-tag key binding a secret to the namespaces entitled to it."""

    def test_defaults_to_brand_when_unset(self, monkeypatch):
        monkeypatch.delenv("RESOURCE_TAG_PREFIX", raising=False)
        assert namespace_tag_key() == "coa:namespace"

    def test_uses_deployment_prefix(self, monkeypatch):
        monkeypatch.setenv("RESOURCE_TAG_PREFIX", "scl")
        assert namespace_tag_key() == "scl:namespace"

    def test_explicit_prefix_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("RESOURCE_TAG_PREFIX", "scl")
        assert namespace_tag_key("acme") == "acme:namespace"

    def test_empty_env_falls_back_to_brand(self, monkeypatch):
        monkeypatch.setenv("RESOURCE_TAG_PREFIX", "")
        assert namespace_tag_key() == "coa:namespace"

    def test_trailing_hyphen_is_stripped(self, monkeypatch):
        """Guards against `{prefix}-{env}-` style values reaching the key."""
        monkeypatch.setenv("RESOURCE_TAG_PREFIX", "scl-")
        assert namespace_tag_key() == "scl:namespace"

    def test_distinct_deployments_get_distinct_keys(self, monkeypatch):
        """Two deployments in one account must not share a binding."""
        assert namespace_tag_key("scl") != namespace_tag_key("coa")


class TestParseNamespaceTag:
    """Tag values are written by whoever owns the secret, so parsing is strict."""

    def test_single_namespace(self):
        assert parse_namespace_tag(_NS_A) == [_NS_A]

    def test_multiple_namespaces_preserve_order(self):
        assert parse_namespace_tag(f"{_NS_A} {_NS_B} {_NS_C}") == [_NS_A, _NS_B, _NS_C]

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "not-a-uuid",
            f"{_NS_A} not-a-uuid",
            f"not-a-uuid {_NS_A}",
            # v1 UUID — namespace ids are v4, and a relaxed check here would let a
            # non-namespace identifier bind a secret.
            "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
            _NS_A.upper(),
        ],
    )
    def test_rejects_non_namespace_entries(self, value):
        with pytest.raises(ValueError):
            parse_namespace_tag(value)

    @pytest.mark.parametrize(
        "value",
        [
            f" {_NS_A}",
            f"{_NS_A} ",
            f"{_NS_A}  {_NS_B}",
            f"{_NS_A}\t{_NS_B}",
            f"{_NS_A}\n{_NS_B}",
        ],
    )
    def test_rejects_non_canonical_separators(self, value):
        """Whitespace IAM cannot match must fail here, not silently at read time.

        The IAM conditions match entries on a literal single space, so a value
        this accepted but IAM could not would pass registration and then break
        every read of the secret.
        """
        with pytest.raises(ValueError):
            parse_namespace_tag(value)


class TestNamespaceTagConditionPatterns:
    """IAM has no word-boundary operator, so entry matching is enumerated."""

    def _matches(self, pattern: str, value: str) -> bool:
        """Evaluate one IAM StringLike pattern (``*`` = zero or more chars)."""
        return re.fullmatch(".*".join(re.escape(p) for p in pattern.split("*")), value) is not None

    def _binds(self, namespace_id: str, value: str) -> bool:
        return any(self._matches(p, value) for p in namespace_tag_condition_patterns(namespace_id))

    @pytest.mark.parametrize(
        "value",
        [
            _NS_A,
            f"{_NS_A} {_NS_B}",
            f"{_NS_B} {_NS_A}",
            f"{_NS_B} {_NS_A} {_NS_C}",
        ],
    )
    def test_matches_every_entry_position(self, value):
        assert self._binds(_NS_A, value)

    def test_does_not_match_a_value_without_the_namespace(self):
        assert not self._binds(_NS_A, f"{_NS_B} {_NS_C}")

    def test_does_not_match_a_substring_occurrence(self):
        """An id embedded in a longer token is not an entry."""
        assert not self._binds(_NS_A, f"prefixed{_NS_A}suffix")
        assert not self._binds(_NS_A, f"prefixed{_NS_A} {_NS_B}")

    def test_every_pattern_a_parsed_value_can_produce_is_covered(self):
        """Cross-check: whatever parse accepts, the IAM patterns must match."""
        for value in (_NS_A, f"{_NS_A} {_NS_B}", f"{_NS_B} {_NS_A}", f"{_NS_B} {_NS_A} {_NS_C}"):
            assert _NS_A in parse_namespace_tag(value)
            assert self._binds(_NS_A, value)


class TestBucketNamespaceTag:
    """The tag a bucket owner sets to authorize namespaces to read that bucket.

    Only a principal holding ``s3:TagResource`` on the bucket can set it, so the
    tag is the evidence that the owner authorized the read. Every case that is not
    an explicit, exact match must deny.
    """

    def test_key_derives_from_tag_prefix(self, monkeypatch):
        monkeypatch.setenv("RESOURCE_TAG_PREFIX", "acme")
        assert bucket_namespace_tag_key() == "acme:namespace"

    def test_key_falls_back_to_brand(self, monkeypatch):
        monkeypatch.delenv("RESOURCE_TAG_PREFIX", raising=False)
        assert bucket_namespace_tag_key() == "coa:namespace"

    def test_key_matches_the_platform_namespace_tag(self, monkeypatch):
        """Same key as ``namespace_tag_key``, deliberately: one tag contract, one
        implementation. The two uses differ in what the tag names — namespaces
        entitled to a secret, versus namespaces a bucket owner authorized to read
        it — not in the key."""
        monkeypatch.delenv("RESOURCE_TAG_PREFIX", raising=False)
        assert bucket_namespace_tag_key() == namespace_tag_key()

    def test_grants_a_listed_namespace(self, monkeypatch):
        monkeypatch.delenv("RESOURCE_TAG_PREFIX", raising=False)
        tags = {"coa:namespace": "ns-a ns-b ns-c"}
        assert bucket_grants_namespace(tags, "ns-a")
        assert bucket_grants_namespace(tags, "ns-b")
        assert bucket_grants_namespace(tags, "ns-c")

    def test_denies_a_namespace_not_listed(self, monkeypatch):
        monkeypatch.delenv("RESOURCE_TAG_PREFIX", raising=False)
        assert not bucket_grants_namespace({"coa:namespace": "ns-a ns-b"}, "ns-z")

    def test_tolerates_untidy_whitespace(self, monkeypatch):
        """Hand-edited tags acquire stray spacing; that must not deny a member."""
        monkeypatch.delenv("RESOURCE_TAG_PREFIX", raising=False)
        tags = {"coa:namespace": "   ns-a    ns-b\tns-c  "}
        assert bucket_grants_namespace(tags, "ns-a")
        assert bucket_grants_namespace(tags, "ns-c")

    def test_matches_whole_entries_not_substrings(self, monkeypatch):
        """A namespace id must not be authorized by sharing a prefix with one."""
        monkeypatch.delenv("RESOURCE_TAG_PREFIX", raising=False)
        assert not bucket_grants_namespace({"coa:namespace": "ns-abc"}, "ns-a")
        assert not bucket_grants_namespace({"coa:namespace": "ns-a"}, "ns-abc")

    def test_denies_when_the_tag_is_absent(self, monkeypatch):
        monkeypatch.delenv("RESOURCE_TAG_PREFIX", raising=False)
        assert not bucket_grants_namespace({}, "ns-a")
        assert not bucket_grants_namespace({"unrelated": "ns-a"}, "ns-a")

    def test_denies_an_empty_tag_value(self, monkeypatch):
        monkeypatch.delenv("RESOURCE_TAG_PREFIX", raising=False)
        assert not bucket_grants_namespace({"coa:namespace": ""}, "ns-a")
        assert not bucket_grants_namespace({"coa:namespace": "   "}, "ns-a")

    def test_denies_an_empty_namespace_id(self, monkeypatch):
        """An empty job namespace must never match, whatever the tag says."""
        monkeypatch.delenv("RESOURCE_TAG_PREFIX", raising=False)
        assert not bucket_grants_namespace({"coa:namespace": "ns-a"}, "")

    def test_one_bucket_can_serve_several_namespaces(self, monkeypatch):
        monkeypatch.delenv("RESOURCE_TAG_PREFIX", raising=False)
        tags = {"coa:namespace": "550e8400-e29b-41d4-a716-446655440000 f47ac10b-58cc-4372-a567-0e02b2c3d479"}
        assert bucket_grants_namespace(tags, "550e8400-e29b-41d4-a716-446655440000")
        assert bucket_grants_namespace(tags, "f47ac10b-58cc-4372-a567-0e02b2c3d479")
        assert not bucket_grants_namespace(tags, "00000000-0000-0000-0000-000000000000")
