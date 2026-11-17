// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * TurtleHighlight — syntax-highlighted Turtle/RDF rendering.
 *
 * Tokenizes Turtle text with regex-based rules and applies color spans.
 * Lightweight alternative to a full CodeMirror/Prism.js setup.
 */
import React from "react";

const COLORS = {
  prefix: "#c4b5fd", // light violet — @prefix, @base
  uri: "#7dd3fc", // light sky — <http://...>
  prefixed: "#5eead4", // light teal — rr:tableName, owl:Class
  keyword: "#fca5a5", // light red — a (rdf:type shorthand)
  literal: "#86efac", // light green — "string literals"
  comment: "#9ca3af", // light grey — # comments
  punctuation: "#94a3b8", // light slate — ; , . [ ]
  datatype: "#d8b4fe", // light purple — ^^xsd:string
};

function tokenize(turtle: string): Array<{ text: string; color?: string }> {
  const tokens: Array<{ text: string; color?: string }> = [];
  const lines = turtle.split("\n");
  for (let li = 0; li < lines.length; li++) {
    const line = lines[li];
    let pos = 0;
    while (pos < line.length) {
      let matched = false;

      if (line[pos] === "#" && (pos === 0 || line[pos - 1] !== "<")) {
        tokens.push({ text: line.slice(pos), color: COLORS.comment });
        pos = line.length;
        matched = true;
      }

      if (!matched && line.slice(pos).match(/^@(prefix|base)\b/)) {
        const m = line.slice(pos).match(/^@(prefix|base)\b/)!;
        tokens.push({ text: m[0], color: COLORS.prefix });
        pos += m[0].length;
        matched = true;
      }

      if (!matched && line[pos] === "<") {
        const end = line.indexOf(">", pos);
        if (end !== -1) {
          tokens.push({ text: line.slice(pos, end + 1), color: COLORS.uri });
          pos = end + 1;
          matched = true;
        }
      }

      if (!matched && line[pos] === '"') {
        let end = pos + 1;
        while (end < line.length && line[end] !== '"') {
          if (line[end] === "\\") end++; // skip escaped
          end++;
        }
        if (end < line.length) end++; // include closing "
        tokens.push({ text: line.slice(pos, end), color: COLORS.literal });
        pos = end;
        matched = true;
      }

      if (!matched && line.slice(pos, pos + 2) === "^^") {
        const m = line.slice(pos).match(/^\^\^[\w:]+/);
        if (m) {
          tokens.push({ text: m[0], color: COLORS.datatype });
          pos += m[0].length;
          matched = true;
        }
      }

      if (
        !matched &&
        line.slice(pos).match(/^a\s/) &&
        (pos === 0 || /\s/.test(line[pos - 1]))
      ) {
        tokens.push({ text: "a", color: COLORS.keyword });
        pos += 1;
        matched = true;
      }

      if (!matched && line.slice(pos).match(/^[\w-]+:[\w-]*/)) {
        const m = line.slice(pos).match(/^[\w-]+:[\w-]*/)!;
        tokens.push({ text: m[0], color: COLORS.prefixed });
        pos += m[0].length;
        matched = true;
      }

      // eslint-disable-next-line no-useless-escape
      if (!matched && /^[;,.\[\]()]/.test(line[pos])) {
        tokens.push({ text: line[pos], color: COLORS.punctuation });
        pos += 1;
        matched = true;
      }

      if (!matched) {
        // eslint-disable-next-line no-useless-escape
        const m = line.slice(pos).match(/^[^\s<"#;,.\[\]()^]+|\s+/);
        if (m) {
          tokens.push({ text: m[0] });
          pos += m[0].length;
        } else {
          tokens.push({ text: line[pos] });
          pos++;
        }
      }
    }
    if (li < lines.length - 1) {
      tokens.push({ text: "\n" });
    }
  }
  return tokens;
}

const ESCAPE_HTML: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
};
function escapeHtml(s: string): string {
  return s.replace(/[&<>]/g, (c) => ESCAPE_HTML[c]);
}

const PRE_STYLE: React.CSSProperties = {
  whiteSpace: "pre-wrap",
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  fontSize: 12,
  lineHeight: 1.5,
  background: "#1e293b",
  color: "#e2e8f0",
  padding: 12,
  borderRadius: 4,
  border: "1px solid #334155",
  overflow: "auto",
  maxHeight: 500,
};

export function TurtleHighlight({ children }: { children: string }) {
  // One HTML string instead of a React <span> per token, which is too many
  // fibers for large Turtle. Token text is HTML-escaped (only fixed colour hex
  // values reach the style attr). Files over 1 MB fall back to plain text.
  const html = React.useMemo(() => {
    if (children.length > 1_000_000) return null;
    return tokenize(children)
      .map((t) =>
        t.color
          ? `<span style="color:${t.color}">${escapeHtml(t.text)}</span>`
          : escapeHtml(t.text),
      )
      .join("");
  }, [children]);

  if (html === null) {
    return <pre style={PRE_STYLE}>{children}</pre>;
  }
  return <pre style={PRE_STYLE} dangerouslySetInnerHTML={{ __html: html }} />;
}
