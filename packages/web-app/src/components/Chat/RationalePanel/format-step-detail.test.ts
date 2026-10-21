// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { formatStepDetail } from "./format-step-detail";
import { formatAppMessage } from "../../../i18n/appMessages";
import type { AppTranslate } from "../../../i18n/LocaleProvider";

const jaTranslate: AppTranslate = (message, values) =>
  formatAppMessage("ja", message, values);

describe("formatStepDetail", () => {
  it("formats application-owned trace details in Japanese", () => {
    expect(
      formatStepDetail(
        "t1.metric_match",
        {
          metricName: "total_revenue",
          matchSource: "exact",
          confidence: 0.95,
        },
        jaTranslate,
      ),
    ).toBe("total_revenue に解決しました（完全一致、信頼度 0.95）。");
    expect(
      formatStepDetail("t2.vkg.firewall", { denied: false }, jaTranslate),
    ).toBe(
      "SQL ファイアウォールを通過しました — 安全な読み取り専用 SELECT です。",
    );
  });

  describe("returns fallback for missing/empty detail", () => {
    it("returns fallback when detail is undefined", () => {
      expect(formatStepDetail("t1.metric_match", undefined)).toBe(
        "Checked metric catalog.",
      );
    });

    it("returns fallback when detail is null", () => {
      expect(formatStepDetail("t1.metric_match", null)).toBe(
        "Checked metric catalog.",
      );
    });

    it("returns fallback when detail is empty object", () => {
      expect(formatStepDetail("t1.metric_match", {})).toBe(
        "Checked metric catalog.",
      );
    });

    it("returns empty string for unknown step with no detail", () => {
      expect(formatStepDetail("unknown_step", undefined)).toBe("");
    });
  });

  describe("t1.metric_match formatter", () => {
    it("formats success with metricName, matchSource, confidence", () => {
      const result = formatStepDetail("t1.metric_match", {
        metricName: "loss_ratio",
        matchSource: "fuzzy",
        confidence: 0.92,
      });
      expect(result).toBe("Resolved to loss_ratio (fuzzy, confidence 0.92).");
    });

    it("uses defaults for matchSource and confidence", () => {
      const result = formatStepDetail("t1.metric_match", {
        metricName: "claim_count",
      });
      expect(result).toBe("Resolved to claim_count (exact, confidence 1).");
    });

    it("formats miss without echoing the query", () => {
      const result = formatStepDetail("t1.metric_match", {
        query: "total revenue",
      });
      expect(result).toBe("No matching metric; routing to structured query.");
    });

    it("treats the {query_length} miss detail as a miss", () => {
      const result = formatStepDetail("t1.metric_match", { query_length: 53 });
      expect(result).toBe("No matching metric; routing to structured query.");
    });

    it("names the qualifier Tier 1 could not apply", () => {
      const result = formatStepDetail("t1.metric_match", {
        metricName: "total_revenue",
        matchSource: "name",
        unhandledQualifier: "gold loyalty tier",
      });
      expect(result).toBe(
        'Matched total_revenue but "gold loyalty tier" can\'t be applied to it; routing to structured query.',
      );
    });

    it("prefers the bypass wording over the resolved wording", () => {
      // A bypass detail carries metricName too (the metric that matched), but it
      // is NOT a resolution — the confidence branch must not win.
      const result = formatStepDetail("t1.metric_match", {
        metricName: "total_revenue",
        unhandledQualifier: "last quarter",
      });
      expect(result).not.toContain("Resolved to");
      expect(result).toContain("last quarter");
    });

    it("falls back to a generic subject when the bypass omits metricName", () => {
      const result = formatStepDetail("t1.metric_match", {
        unhandledQualifier: "north region",
      });
      expect(result).toBe(
        'Matched a metric but "north region" can\'t be applied to it; routing to structured query.',
      );
    });

    // The sibling bypass. The backend records match_count > 1 as a bare STRING
    // ("3 metrics"), so the object formatter never sees it and the raw fragment
    // used to reach the UI verbatim.
    it("explains the multi-metric bypass instead of showing the raw count", () => {
      const result = formatStepDetail("t1.metric_match", "3 metrics");
      expect(result).toBe(
        "Question matched 3 metrics, so it is ambiguous for the single-metric path; routing to structured query.",
      );
      expect(result).not.toBe("3 metrics");
    });

    it("pluralizes the count rather than interpolating it bare", () => {
      // The backend emits f"{count} metrics" unconditionally, so relying on its
      // wording produced "matched 1 metrics".
      expect(formatStepDetail("t1.metric_match", "1 metric")).toBe(
        "Question matched 1 metric, so it is ambiguous for the single-metric path; routing to structured query.",
      );
      expect(formatStepDetail("t1.metric_match", "1 metrics")).toContain(
        "matched 1 metric,",
      );
    });

    it("leaves an unrelated string detail alone", () => {
      // Guard the regex: only "<n> metric(s)" is the bypass shape.
      expect(formatStepDetail("t1.metric_match", "3 metrics matched foo")).toBe(
        "3 metrics matched foo",
      );
    });
  });

  describe("t2.sql.generate error humanization", () => {
    // The backend records this step's FAILURE detail as a bare string
    // (nl_to_sql/strategy.py records detail=err), not an object — so these
    // string cases are the ones that actually reach the UI in production.
    it("humanizes the retrieve_failed proxy error string as a fallthrough", () => {
      const result = formatStepDetail(
        "t2.sql.generate",
        "retrieve_failed: Vector search proxy returned an error",
      );
      expect(result).toBe(
        "NL→SQL skipped (the search backend was unavailable); falling back to VKG.",
      );
    });

    it("humanizes the empty_sql error string via the code label", () => {
      const result = formatStepDetail("t2.sql.generate", "empty_sql");
      expect(result).toBe(
        "NL→SQL skipped (the model returned no SQL); falling back to VKG.",
      );
    });

    it("humanizes an unmapped snake_case error string", () => {
      const result = formatStepDetail("t2.sql.generate", "generate_failed");
      expect(result).toBe(
        "NL→SQL skipped (generate failed); falling back to VKG.",
      );
    });

    it("also humanizes the object error shape (defensive)", () => {
      const result = formatStepDetail("t2.sql.generate", {
        error: "retrieve_failed: Vector search proxy returned an error",
      });
      expect(result).toBe(
        "NL→SQL skipped (the search backend was unavailable); falling back to VKG.",
      );
    });

    it("formats the success object shape (not the skipped copy)", () => {
      const result = formatStepDetail("t2.sql.generate", {
        confidence: 0.9,
        tables: ["claim", "policy"],
      });
      expect(result).not.toContain("skipped");
      expect(result).toContain("0.9");
    });
  });

  describe("query.embed formatter", () => {
    it("formats success with dimensions", () => {
      const result = formatStepDetail("query.embed", { dimensions: 1536 });
      expect(result).toBe("Embedded query (1536 dimensions).");
    });

    it("formats error with message", () => {
      const result = formatStepDetail("query.embed", {
        error: "ModelTimeout",
        message: "Embedding model timed out",
      });
      expect(result).toBe("Embedding failed: Embedding model timed out");
    });

    it("formats error without message (uses error string)", () => {
      const result = formatStepDetail("query.embed", {
        error: "ServiceUnavailable",
      });
      expect(result).toBe("Embedding failed: ServiceUnavailable");
    });
  });

  describe("t1.execute formatter", () => {
    it("formats with rowCount and table", () => {
      const result = formatStepDetail("t1.execute", {
        rowCount: 42,
        table: "claims",
      });
      expect(result).toBe("42 row(s) returned from claims.");
    });

    it("appends truncated indicator", () => {
      const result = formatStepDetail("t1.execute", {
        rowCount: 1000,
        table: "policies",
        truncated: true,
      });
      expect(result).toBe("1000 row(s) returned from policies. (truncated)");
    });

    it("uses defaults for missing fields", () => {
      const result = formatStepDetail("t1.execute", {});
      expect(result).toBe("0 row(s) returned.");
    });
  });

  describe("t2.strategy_selected formatter", () => {
    it("renders the chosen strategy with a friendly name", () => {
      expect(
        formatStepDetail("t2.strategy_selected", { strategy: "ontop" }),
      ).toBe("Chose VKG (Ontop).");
      expect(
        formatStepDetail("t2.strategy_selected", { strategy: "nl_to_sql" }),
      ).toBe("Chose NL→SQL.");
    });

    it("falls back gracefully for an unknown/absent strategy", () => {
      expect(formatStepDetail("t2.strategy_selected", {})).toBe(
        "Strategy selected.",
      );
    });
  });

  describe("table-list capping and error suppression", () => {
    it("caps a long table list to 5 with +N more", () => {
      const result = formatStepDetail("t2.sql.generate", {
        confidence: 0.2,
        tables: ["a", "b", "c", "d", "e", "f", "g"],
      });
      expect(result).toBe(
        "SQL generated (confidence 0.2, tables: a, b, c, d, e, +2 more).",
      );
    });

    it("shows full list when 5 or fewer", () => {
      const result = formatStepDetail("t2.sql.generate", {
        confidence: 0.5,
        tables: ["a", "b", "c"],
      });
      expect(result).toBe("SQL generated (confidence 0.5, tables: a, b, c).");
    });

    it("suppresses a raw exception class name on vkg compile failure", () => {
      const result = formatStepDetail("t2.vkg.compile", {
        error: "VKGTranslationError",
      });
      expect(result).toBe("Ontop could not translate SPARQL to SQL.");
    });

    it("keeps a meaningful error code on vkg compile failure", () => {
      const result = formatStepDetail("t2.vkg.compile", {
        error: "vkg_translation_error",
      });
      expect(result).toBe(
        "Ontop could not translate SPARQL to SQL: Ontop could not translate the SPARQL to SQL.",
      );
    });
  });

  describe("t1.authorize formatter", () => {
    it("formats allow decision", () => {
      const result = formatStepDetail("t1.authorize", {
        principal: "user::alice",
        decision: "allow",
      });
      expect(result).toBe(
        "Access authorized for user::alice (no row/column restrictions).",
      );
    });

    it("formats deny decision", () => {
      const result = formatStepDetail("t1.authorize", {
        principal: "user::bob",
        decision: "deny",
      });
      expect(result).toBe("Access denied for principal user::bob.");
    });

    it("uses unknown for missing principal", () => {
      const result = formatStepDetail("t1.authorize", {
        decision: "allow",
      });
      expect(result).toBe(
        "Access authorized for unknown (no row/column restrictions).",
      );
    });
  });

  describe("firewall formatter", () => {
    it("formats the VKG pass path ({denied:false}) as passed", () => {
      const result = formatStepDetail("t2.vkg.firewall", { denied: false });
      expect(result).toBe(
        "SQL firewall passed — query is a safe read-only SELECT.",
      );
    });

    it("formats the authorized path as passed", () => {
      const result = formatStepDetail("t2.sql.firewall", {
        decision: "authorized",
      });
      expect(result).toBe(
        "SQL firewall passed — query is a safe read-only SELECT.",
      );
    });

    it("formats a block ({denied:true})", () => {
      const result = formatStepDetail("t2.vkg.firewall", { denied: true });
      expect(result).toBe("SQL firewall blocked the query.");
    });

    it("humanizes an error detail (UnsafeSQLError) instead of showing the raw name", () => {
      const result = formatStepDetail("t2.vkg.firewall", {
        error: "UnsafeSQLError",
      });
      expect(result).toBe(
        "SQL firewall blocked the query — the generated SQL was not a safe read-only SELECT.",
      );
    });

    it("falls back to passed when no detail", () => {
      const result = formatStepDetail("t2.vkg.firewall", undefined);
      expect(result).toBe("SQL firewall passed.");
    });
  });

  describe("lexical_baseline_retrieve formatter", () => {
    it("parses the backend success string into a clean sentence", () => {
      const result = formatStepDetail(
        "lexical_baseline_retrieve",
        "strategy=chunk_based_semantic; 5 nodes retrieved",
      );
      expect(result).toBe("Retrieved 5 document chunk(s).");
    });

    it("parses a graph error string as a retrieval failure", () => {
      const result = formatStepDetail(
        "lexical_baseline_retrieve",
        "strategy=chunk_based_semantic; GraphQueryError",
      );
      expect(result).toBe(
        "No documents retrieved — the graph store did not respond in time.",
      );
    });

    it("parses a timeout string as a retrieval failure", () => {
      const result = formatStepDetail(
        "lexical_baseline_retrieve",
        "strategy=chunk_based_semantic; TimeoutError after 15.0s",
      );
      expect(result).toBe(
        "No documents retrieved — the graph store timed out.",
      );
    });

    it("formats a structured detail object", () => {
      const result = formatStepDetail("lexical_baseline_retrieve", {
        nodes: 5,
        strategy: "chunk_based_semantic",
      });
      expect(result).toBe(
        "Retrieved 5 document chunk(s) via chunk_based_semantic.",
      );
    });

    it("falls back when no detail", () => {
      const result = formatStepDetail("lexical_baseline_retrieve", undefined);
      expect(result).toBe("Document chunks retrieved.");
    });
  });

  describe("t2.vkg.translate formatter", () => {
    it("formats with dialect and tables array", () => {
      const result = formatStepDetail("t2.vkg.translate", {
        dialect: "Athena",
        tables: ["claims", "policies"],
      });
      expect(result).toBe("Dialect=Athena. Tables: claims, policies.");
    });

    it("formats with dialect only", () => {
      const result = formatStepDetail("t2.vkg.translate", {
        dialect: "Presto",
      });
      expect(result).toBe("Dialect=Presto.");
    });

    it("formats with tables string (non-array)", () => {
      const result = formatStepDetail("t2.vkg.translate", { tables: "claims" });
      expect(result).toBe("Tables: claims.");
    });
  });

  describe("legacy string details", () => {
    it("passes through clean strings", () => {
      expect(
        formatStepDetail("t1.metric_match", "Query matched successfully"),
      ).toBe("Query matched successfully");
    });

    it("converts snake_case strings to Title Case", () => {
      expect(formatStepDetail("t1.metric_match", "query_executed_ok")).toBe(
        "Query Executed Ok",
      );
    });
  });

  describe("noise filtering", () => {
    it('filters "ok"', () => {
      expect(formatStepDetail("t1.metric_match", "ok")).toBe(
        "Checked metric catalog.",
      );
    });

    it('filters "empty"', () => {
      expect(formatStepDetail("query.embed", "empty")).toBe("Query embedded.");
    });

    it('filters "{}"', () => {
      expect(formatStepDetail("t1.execute", "{}")).toBe("Query executed.");
    });

    it('filters "[]"', () => {
      expect(formatStepDetail("t2.vkg.translate", "[]")).toBe(
        "SPARQL generated.",
      );
    });

    it("is case-insensitive for noise values", () => {
      expect(formatStepDetail("t1.metric_match", "OK")).toBe(
        "Checked metric catalog.",
      );
    });
  });

  describe("Python-style dicts (single quotes)", () => {
    it("extracts message from Python dict", () => {
      // error field takes precedence over message in extraction order
      const pyDict = "{'error': 'timeout', 'message': 'Connection timed out'}";
      expect(formatStepDetail("t1.execute", pyDict)).toBe("timeout");
    });

    it("extracts message when only message is present", () => {
      const pyDict = "{'message': 'Connection timed out'}";
      expect(formatStepDetail("t1.execute", pyDict)).toBe(
        "Connection timed out",
      );
    });

    it("returns fallback for empty Python dict values", () => {
      const pyDict = "{'items': [], 'rows': []}";
      expect(formatStepDetail("t1.metric_match", pyDict)).toBe(
        "Checked metric catalog.",
      );
    });
  });

  describe("unknown structured objects", () => {
    it("extracts message from unknown step", () => {
      const result = formatStepDetail("unknown_step", {
        message: "Something happened",
      });
      expect(result).toBe("Something happened");
    });

    it("extracts error from unknown step", () => {
      const result = formatStepDetail("unknown_step", {
        error: "Unexpected failure",
      });
      expect(result).toBe("Unexpected failure");
    });

    it("extracts reason from unknown step", () => {
      const result = formatStepDetail("unknown_step", {
        reason: "Rate limited",
      });
      expect(result).toBe("Rate limited");
    });

    it("returns fallback for step with no extractable fields", () => {
      const result = formatStepDetail("t1.metric_match", {
        foo: 123,
        bar: true,
      });
      expect(result).toBe("Checked metric catalog.");
    });
  });
});
