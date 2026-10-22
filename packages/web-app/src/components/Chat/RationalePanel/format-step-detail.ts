// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  formatAppMessage,
  type AppMessage,
  type AppMessageValues,
} from "../../../i18n/appMessages";
import type { AppTranslate } from "../../../i18n/LocaleProvider";

/**
 * Per-step-type formatters that convert structured detail objects
 * from the backend into human-readable sentences for the execution plan.
 *
 * Each formatter receives the detail record and returns a display string.
 * Falls back to raw string detail or empty string for unknown shapes.
 */

type DetailRecord = Record<string, unknown>;
type Formatter = (detail: DetailRecord, t: AppTranslate) => string;

const englishTranslate: AppTranslate = (
  message: AppMessage,
  values?: AppMessageValues,
) => formatAppMessage("en", message, values);

// Human-readable phrasing for raw backend error codes so the UI never shows
// snake_case identifiers like "vkg_translation_error".
const ERROR_CODE_LABELS: Readonly<Record<string, AppMessage>> = {
  vkg_translation_error: "Ontop could not translate the SPARQL to SQL",
  no_tables_retrieved: "no matching tables found",
  query_execution_error: "query execution failed",
  firewall_error: "firewall check failed",
  unsafe_sql: "generated SQL was not a safe SELECT",
  empty_sql: "the model returned no SQL",
  // Backend exception class names that reach the trace verbatim — map to a
  // reader-friendly cause so the demo never shows a raw "FooError".
  UnsafeSQLError: "the generated SQL was not a safe read-only SELECT",
  GraphQueryError: "the graph store did not respond in time",
  TimeoutError: "the request timed out",
  RetrieveError: "the search backend was unavailable",
};

function humanizeError(value: unknown, t: AppTranslate): string {
  const raw = String(value ?? "").trim();
  const knownLabel = ERROR_CODE_LABELS[raw];
  if (knownLabel) return t(knownLabel);
  // Some backend errors arrive as "<code>: <detail>" (e.g.
  // "retrieve_failed: Vector search proxy returned an error"). Humanize the
  // leading code and keep a short, readable tail rather than leaking the raw
  // internal message.
  const prefixed = raw.match(/^([a-z][a-z0-9_]+):\s*(.+)$/);
  if (prefixed) {
    const code = prefixed[1];
    if (code === "retrieve_failed")
      return t("the search backend was unavailable");
    const label = ERROR_CODE_LABELS[code];
    return label ? t(label) : code.replace(/_/g, " ");
  }
  // Generic snake_case → sentence case for unknown codes.
  if (/^[a-z][a-z0-9_]+$/.test(raw)) return raw.replace(/_/g, " ");
  return raw;
}

// A bare exception class name (e.g. "VKGTranslationError") carries no useful
// info for end users — used to decide whether to append an error detail.
function isRawExceptionName(value: unknown): boolean {
  const raw = String(value ?? "").trim();
  return /^[A-Z][A-Za-z0-9]*(Error|Exception)$/.test(raw);
}

// Cap a list for inline display; full list stays in the raw detail/trace.
function summarizeList(items: unknown, t: AppTranslate, max = 5): string {
  if (!Array.isArray(items) || items.length === 0) return "";
  const shown = items.slice(0, max).join(", ");
  const extra = items.length - max;
  return extra > 0
    ? t("{items}, +{count} more", { items: shown, count: extra })
    : shown;
}

const _authorizeFormatter: Formatter = (d, t) => {
  const principal = d.principal ?? "unknown";
  // "allow" with no row/column restriction to apply. Phrased as an outcome
  // ("access authorized") rather than the implementation detail ("no filter
  // injection required"), which read as a non-sequitur in the demo trace.
  if (d.decision === "allow")
    return t(
      "Access authorized for {principal} (no row/column restrictions).",
      { principal: String(principal) },
    );
  if (d.decision === "deny")
    return t("Access denied for principal {principal}.", {
      principal: String(principal),
    });
  return "";
};

const _firewallFormatter: Formatter = (d, t) => {
  // Backend emits {denied: false} on the pass path (t2.vkg.firewall) and
  // {decision: "authorized"} elsewhere. The firewall validates the SQL — it
  // does not "inject" — so a clean pass reads as "passed", not "no injection".
  if (d.denied === false || d.decision === "authorized")
    return t("SQL firewall passed — query is a safe read-only SELECT.");
  // Error path: the firewall rejected the generated SQL (e.g. a low-confidence
  // SPARQL translated to non-SELECT SQL). Explain the cause rather than showing
  // the raw exception name; this is a non-fatal fallthrough to Tier 3.
  if (d.error)
    return t("SQL firewall blocked the query — {reason}.", {
      reason: humanizeError(d.error, t),
    });
  if (d.denied === true) return t("SQL firewall blocked the query.");
  if (d.reason) return String(d.reason);
  return "";
};

const _executeFormatter: Formatter = (d, t) => {
  const rows = d.rowCount ?? d.rows ?? 0;
  const tables = Array.isArray(d.tables)
    ? summarizeList(d.tables, t)
    : d.table
      ? String(d.table)
      : "";
  let msg = tables
    ? t("{rows} row(s) returned from {tables}.", {
        rows: String(rows),
        tables,
      })
    : t("{rows} row(s) returned.", { rows: String(rows) });
  if (d.truncated) msg += t(" (truncated)");
  return msg;
};

const stepFormatters: Record<string, Formatter> = {
  "t1.metric_match": (d, t) => {
    // Tier-1 matched a metric but the question carried a qualifier the metric's
    // fixed SQL cannot express (a filter, grouping, or time window), so it
    // declined rather than return the unfiltered aggregate. Checked before the
    // metricName branch — a bypass is not a resolution and carries no confidence.
    if (d.unhandledQualifier) {
      return t(
        'Matched {metric} but "{qualifier}" can\'t be applied to it; routing to structured query.',
        {
          metric: String(d.metricName ?? t("a metric")),
          qualifier: String(d.unhandledQualifier),
        },
      );
    }
    if (d.metricName) {
      const matchSource = String(d.matchSource ?? "exact");
      return t("Resolved to {metric} ({source}, confidence {confidence}).", {
        metric: String(d.metricName),
        source: matchSource === "exact" ? t("exact") : matchSource,
        confidence: String(d.confidence ?? 1.0),
      });
    }
    // No metric matched — don't echo the query (it's already on screen above);
    // state what happens next instead. The backend miss detail is
    // {query_length: N} (no metricName), so treat any non-match detail as a miss.
    if (
      d.query !== undefined ||
      d.matchCount !== undefined ||
      d.query_length !== undefined
    ) {
      return t("No matching metric; routing to structured query.");
    }
    return "";
  },

  "query.embed": (d, t) => {
    if (d.dimensions)
      return t("Embedded query ({count} dimensions).", {
        count: String(d.dimensions),
      });
    if (d.error)
      return t("Embedding failed: {error}", {
        error: String(d.message || d.error),
      });
    return "";
  },

  "t2.vkg.context": (d, t) => {
    const classes = d.classCount ?? d.classes ?? 0;
    const props = d.propertyCount ?? d.properties ?? 0;
    return t(
      "{classes} classes, {properties} properties pulled from the published ontology.",
      { classes: String(classes), properties: String(props) },
    );
  },

  "t2.vkg.translate": (d, t) => {
    if (d.confidence)
      return t("NL → SPARQL with confidence {confidence}.", {
        confidence: String(d.confidence),
      });
    const parts: string[] = [];
    if (d.dialect)
      parts.push(t("Dialect={dialect}", { dialect: String(d.dialect) }));
    if (d.tables) {
      const tables = Array.isArray(d.tables)
        ? d.tables.join(", ")
        : String(d.tables);
      parts.push(t("Tables: {tables}", { tables }));
    }
    return parts.length > 0 ? `${parts.join(". ")}.` : "";
  },

  "t2.vkg.validate": (d, t) => {
    if (d.valid === true || d.status === "clean") return t("Parsed clean.");
    if (d.error)
      return t("Validation error: {error}", { error: String(d.error) });
    return "";
  },

  "t2.vkg.retry": (d, t) => {
    if (d.reason) {
      // Strip the "retry also failed: <code>" wrapper and humanize the code.
      const reason = String(d.reason).replace(/^retry also failed:\s*/i, "");
      return t("Retry failed — {reason}.", {
        reason: humanizeError(reason, t),
      });
    }
    if (d.error)
      return t("Retry failed — {reason}.", {
        reason: humanizeError(d.error, t),
      });
    return "";
  },

  "t2.vkg.compile": (d, t) => {
    // Ontop SPARQL→SQL translation. On failure this is what triggers the retry.
    // Suppress a bare exception class name (no user value); the base message
    // already explains the failure.
    if (d.error) {
      return isRawExceptionName(d.error)
        ? t("Ontop could not translate SPARQL to SQL.")
        : t("Ontop could not translate SPARQL to SQL: {reason}.", {
            reason: humanizeError(d.error, t),
          });
    }
    const parts: string[] = [];
    if (d.dialect)
      parts.push(t("dialect {dialect}", { dialect: String(d.dialect) }));
    const tables = summarizeList(d.tables, t);
    if (tables) parts.push(t("tables: {tables}", { tables }));
    return parts.length > 0
      ? t("Translated to SQL ({details}).", { details: parts.join(", ") })
      : t("Translated SPARQL to SQL.");
  },

  "t2.sql.generate": (d, t) => {
    // Error path: the NL→SQL strategy couldn't produce SQL (e.g. its schema
    // retrieval was unavailable). This is a non-fatal fallthrough — the VKG
    // (Ontop) strategy runs next — so phrase it as "skipped", not a hard error.
    if (d.error) {
      return t("NL→SQL skipped ({reason}); falling back to VKG.", {
        reason: humanizeError(d.error, t),
      });
    }
    const conf = d.confidence;
    const tables = summarizeList(d.tables, t);
    const parts: string[] = [];
    if (conf !== undefined)
      parts.push(t("confidence {confidence}", { confidence: String(conf) }));
    if (tables) parts.push(t("tables: {tables}", { tables }));
    return parts.length > 0
      ? t("SQL generated ({details}).", { details: parts.join(", ") })
      : t("SQL generated.");
  },

  "t1.authorize": _authorizeFormatter,
  "t2.sql.authorize": _authorizeFormatter,
  "t2.vkg.authorize": _authorizeFormatter,

  "t1.firewall": _firewallFormatter,
  "t2.sql.firewall": _firewallFormatter,
  "t2.vkg.firewall": _firewallFormatter,

  "t1.execute": _executeFormatter,
  "t2.sql.execute": _executeFormatter,
  "t2.vkg.execute": _executeFormatter,

  "t3.vector_search": (d, t) => {
    if (d.chunks !== undefined)
      return t("{count} chunks retrieved.", { count: String(d.chunks) });
    if (d.itemCount !== undefined)
      return t("{count} items retrieved.", { count: String(d.itemCount) });
    return "";
  },

  "t3.graph_traverse": (d, t) => {
    if (d.entities !== undefined)
      return t("{count} entities found.", { count: String(d.entities) });
    if (d.itemCount !== undefined)
      return t("{count} items.", { count: String(d.itemCount) });
    return "";
  },

  "t3.synthesize": (d, t) => {
    if (d.outputTokens)
      return t("{input} → {output} tokens.", {
        input: String(d.inputTokens ?? "?"),
        output: String(d.outputTokens),
      });
    if (d.tokenCount)
      return t("Generated {count} tokens.", {
        count: String(d.tokenCount),
      });
    return "";
  },

  "t3.retrieve": (d, t) => {
    const count = Array.isArray(d.sources) ? d.sources.length : 0;
    return count
      ? t("{count} sources in parallel.", { count })
      : t("Parallel retrieval.");
  },

  // graphrag-toolkit lexical retriever (TIER3_STRATEGY=lexical-baseline).
  // Backend sends a STRING detail ("strategy=chunk_based_semantic; 5 nodes
  // retrieved"), which formatStepDetail passes through directly — this handler
  // only fires if the detail is ever upgraded to a structured object.
  lexical_baseline_retrieve: (d, t) => {
    const nodes = d.nodes ?? d.itemCount ?? d.count;
    if (nodes !== undefined)
      return d.strategy
        ? t("Retrieved {count} document chunk(s) via {strategy}.", {
            count: String(nodes),
            strategy: String(d.strategy),
          })
        : t("Retrieved {count} document chunk(s).", {
            count: String(nodes),
          });
    if (d.error)
      return t("Lexical retrieval failed: {reason}.", {
        reason: humanizeError(d.error, t),
      });
    return "";
  },

  "t2.sql.failed": (d, t) => strategyFailedDetail(d, t),
  "t2.vkg.failed": (d, t) => strategyFailedDetail(d, t),

  "t2.strategy_selected": (d, t) => {
    const friendly = STRATEGY_DISPLAY_NAMES[String(d.strategy)] ?? d.strategy;
    return friendly
      ? t("Chose {strategy}.", { strategy: String(friendly) })
      : t("Strategy selected.");
  },
};

/** Human-friendly names for the Tier-2 strategy identifiers. */
const STRATEGY_DISPLAY_NAMES: Record<string, string> = {
  nl_to_sql: "NL→SQL",
  ontop: "VKG (Ontop)",
};

/** Detail for an abandoned-strategy step: explain why it was skipped. */
function strategyFailedDetail(d: DetailRecord, t: AppTranslate): string {
  if (d.reason === "empty_low_confidence") {
    const conf =
      typeof d.confidence === "number"
        ? t(" (confidence {confidence})", {
            confidence: String(d.confidence),
          })
        : "";
    return t(
      "Returned no rows with low confidence{confidence} — trying another approach.",
      { confidence: conf },
    );
  }
  if (d.message)
    return t("Abandoned — {reason}.", {
      reason: humanizeError(d.message, t),
    });
  if (d.error)
    return t("Abandoned — {reason}.", {
      reason: humanizeError(d.error, t),
    });
  return t("Strategy abandoned; trying another approach.");
}

const NOISE_VALUES = new Set(["ok", "empty", "null", "{}", "[]", "none"]);

const STEP_FALLBACKS = {
  "t1.metric_match": "Checked metric catalog.",
  "query.embed": "Query embedded.",
  "t2.vkg.context": "Ontology context loaded.",
  "t2.vkg.translate": "SPARQL generated.",
  "t2.vkg.validate": "Passed.",
  "t2.vkg.compile": "Translated SPARQL to SQL.",
  "t2.sql.generate": "SQL generated.",
  "t1.authorize": "Authorized.",
  "t2.sql.authorize": "Authorized.",
  "t2.vkg.authorize": "Authorized.",
  "t1.firewall": "SQL firewall passed.",
  "t2.sql.firewall": "SQL firewall passed.",
  "t2.vkg.firewall": "SQL firewall passed.",
  "t1.execute": "Query executed.",
  "t2.sql.execute": "Query executed.",
  "t2.vkg.execute": "Query executed.",
  "t3.vector_search": "Knowledge base searched.",
  "t3.graph_traverse": "Graph traversed.",
  "t3.synthesize": "Response synthesized.",
  "t3.retrieve": "Parallel retrieval complete.",
  lexical_baseline_retrieve: "Document chunks retrieved.",
  "t3.catalog_context": "Catalog context loaded.",
  "t2.vkg.retry": "Retry attempted.",
  "t2.sql.failed": "NL→SQL strategy abandoned.",
  "t2.vkg.failed": "VKG strategy abandoned.",
  "t2.strategy_selected": "Strategy selected.",
  "routing.select": "Intent routed.",
} satisfies Record<string, AppMessage>;

/**
 * Format a trace step's detail for display in the execution plan.
 *
 * Always returns a non-empty string — uses fallback descriptions when
 * no structured detail is available from the backend.
 */
export function formatStepDetail(
  stepName: string,
  detail: string | Record<string, unknown> | undefined | null,
  translate: AppTranslate = englishTranslate,
): string {
  const fallback = STEP_FALLBACKS[stepName as keyof typeof STEP_FALLBACKS];
  if (!detail) return fallback ? translate(fallback) : "";

  // Structured detail object — use formatter
  if (typeof detail === "object") {
    const formatter = stepFormatters[stepName];
    if (formatter) {
      const result = formatter(detail, translate);
      if (result) return result;
    }
    // Generic error/message extraction
    if (detail.message && typeof detail.message === "string") {
      return String(detail.message);
    }
    if (detail.error && typeof detail.error === "string") {
      return String(detail.error);
    }
    if (detail.reason && typeof detail.reason === "string") {
      return String(detail.reason);
    }
    return fallback ? translate(fallback) : "";
  }

  // String detail — clean up noise
  const trimmed = detail.trim();
  if (NOISE_VALUES.has(trimmed.toLowerCase())) {
    return fallback ? translate(fallback) : "";
  }

  // Some backend steps send terse string details; turn them into sentences.
  if (stepName === "t2.vkg.context") {
    const m = trimmed.match(/(\d+)\s+classes?,\s*(\d+)\s+properties?/i);
    if (m)
      return translate(
        "{classes} classes, {properties} properties pulled from the published ontology.",
        { classes: m[1], properties: m[2] },
      );
  }
  if (stepName === "t2.vkg.translate") {
    const m = trimmed.match(/confidence=([\d.]+)/i);
    if (m)
      return translate("NL → SPARQL with confidence {confidence}.", {
        confidence: m[1],
      });
  }
  // The OTHER Tier-1 bypass. When more than one governed metric matches, the
  // question is ambiguous for the single-metric path, so Tier 1 declines rather
  // than execute just the first one. The backend records this as a BARE STRING
  // ("3 metrics"), not an object, so the stepFormatters entry above never sees it
  // and the raw fragment reached the UI verbatim. Phrase it like its sibling
  // residual_qualifier_bypass: name the reason, then where the question goes.
  // The count is pluralized rather than interpolated bare: the backend emits
  // f"{count} metrics" unconditionally, so a hypothetical "1 metric" detail would
  // otherwise render "matched 1 metrics".
  if (stepName === "t1.metric_match") {
    const m = trimmed.match(/^(\d+)\s+metrics?$/i);
    if (m) {
      const n = Number(m[1]);
      return translate(
        n === 1
          ? "Question matched {count} metric, so it is ambiguous for the single-metric path; routing to structured query."
          : "Question matched {count} metrics, so it is ambiguous for the single-metric path; routing to structured query.",
        { count: n },
      );
    }
  }
  // The NL→SQL generate step records its failure detail as a BARE STRING
  // (e.g. "retrieve_failed: Vector search proxy returned an error" or
  // "empty_sql"), not an object — so the object formatter above never sees it.
  // Humanize it here so the raw backend string never reaches the UI. This is a
  // non-fatal fallthrough (VKG/Ontop runs next); the trace status already
  // conveys skipped-vs-error, this is just the copy.
  if (stepName === "t2.sql.generate") {
    return translate("NL→SQL skipped ({reason}); falling back to VKG.", {
      reason: humanizeError(trimmed, translate),
    });
  }

  // Lexical retriever sends a string detail: "strategy=<name>; <N nodes
  // retrieved | TimeoutError after Xs | GraphQueryError>". Parse it into a
  // human sentence. On error the retriever returns no chunks (the graphrag
  // engine does vector + Neptune content-fetch as one call), so synthesis runs
  // without retrieved context — phrase it as a retrieval failure, not a
  // partial success.
  if (stepName === "lexical_baseline_retrieve") {
    const outcome = trimmed.replace(/^strategy=[^;]*;\s*/i, "");
    const nodeMatch = outcome.match(/^(\d+)\s+nodes?\s+retrieved/i);
    if (nodeMatch)
      return translate("Retrieved {count} document chunk(s).", {
        count: nodeMatch[1],
      });
    if (/timeouterror/i.test(outcome))
      return translate("No documents retrieved — the graph store timed out.");
    if (/^[A-Za-z]+Error/.test(outcome))
      return translate("No documents retrieved — {reason}.", {
        reason: humanizeError(outcome.match(/^[A-Za-z]+Error/)?.[0], translate),
      });
    return outcome || (fallback ? translate(fallback) : "");
  }

  // Python-style dicts or JSON — try to extract message
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      const jsonStr = trimmed.includes("'")
        ? trimmed.replace(/'/g, '"')
        : trimmed;
      const parsed = JSON.parse(jsonStr);
      if (typeof parsed === "object" && parsed !== null) {
        const msg =
          parsed.error || parsed.message || parsed.detail || parsed.reason;
        if (typeof msg === "string") return msg;
        const values = Object.values(parsed);
        if (values.every((v) => !v || (Array.isArray(v) && v.length === 0))) {
          return fallback ? translate(fallback) : "";
        }
      }
      return fallback ? translate(fallback) : "";
    } catch {
      return fallback ? translate(fallback) : "";
    }
  }

  // Snake_case identifiers → Title Case
  if (/^[a-z][a-z0-9_]+$/.test(trimmed)) {
    return trimmed.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  return trimmed;
}
