// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * TokenInput — a reusable "chip input" for editing a list of short string
 * values (tags/tokens).
 *
 * The user types a value and commits it with Enter or the add button; each
 * committed value is rendered as a dismissable chip via Cloudscape's
 * `TokenGroup`. Values are trimmed and de-duplicated.
 *
 * Committed text is split on commas and tabs, so a list can be pasted in one
 * go. Without that, pasting `"Dress, Coat, Shirt"` produced a single token
 * spelled `"Dress, Coat, Shirt"` — it passes length and duplicate checks, so
 * nothing flagged it and the caller silently received one nonsense entry. Every
 * consumer holds identifier-like values (column, table and metric names, entity
 * classes, topic names), none of which can legitimately contain those
 * characters, so treating them as separators is always the right reading.
 *
 * A newline-separated list cannot be handled here: a single-line `<input>`
 * applies the HTML value sanitization algorithm, which strips CR and LF, so such
 * a paste arrives already concatenated and the entry boundaries are gone before
 * any of this code runs. Accepting one needs a `<textarea>`, which is a
 * different control, not a change to this one.
 */
import React, { useState } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import TokenGroup from "@cloudscape-design/components/token-group";

/**
 * Separators recognised when splitting committed text.
 *
 * A run collapses to one break so `"a,, b"` and a trailing comma do not yield
 * empty entries. Semicolons are deliberately excluded: they are rare in pasted
 * lists and plausible inside a value. CR and LF are matched for completeness,
 * but a single-line input strips them before this runs — see the note above.
 */
const ENTRY_SEPARATORS = /[,\n\r\t]+/;

export interface TokenInputProps {
  /** Current committed values, rendered as chips. */
  value: string[];
  /** Called with the next value list whenever a chip is added or removed. */
  onChange: (value: string[]) => void;
  /** Placeholder shown in the text input. */
  placeholder?: string;
  /** Accessible label for the text input. */
  ariaLabel?: string;
  /** Disables the input and the add button. */
  disabled?: boolean;
  /**
   * Optional validator for a candidate value. Return an error string to reject
   * the value (it won't be added and the message is shown), or `null` to allow.
   *
   * Called once per entry when several are committed at once, each time with the
   * entries accepted so far, so cap and duplicate checks see the running list
   * rather than only what was there before the paste.
   */
  validate?: (candidate: string, existing: string[]) => string | null;
  /**
   * Plural noun for the values, used in the multi-entry confirmation
   * ("Added 3 classes."). Only ever rendered in the plural.
   */
  itemLabelPlural?: string;
}

export const TokenInput: React.FC<TokenInputProps> = ({
  value,
  onChange,
  placeholder,
  ariaLabel,
  disabled = false,
  validate,
  itemLabelPlural = "entries",
}) => {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const clearMessages = () => {
    setError(null);
    setNotice(null);
  };

  /**
   * Commit the input, which may hold one entry or a pasted list.
   *
   * A single entry keeps the original behaviour exactly: on rejection the text
   * is left in place so it can be corrected. A multi-entry commit instead adds
   * everything that passes and clears the field, because leaving text that was
   * partly consumed would be worse than reporting what was skipped.
   */
  const addTokens = () => {
    const entries = text
      .split(ENTRY_SEPARATORS)
      .map((entry) => entry.trim())
      .filter((entry) => entry.length > 0);
    if (entries.length === 0) {
      setText("");
      clearMessages();
      return;
    }

    const accepted: string[] = [];
    const rejections: string[] = [];
    // Membership set over already-present values, so the per-entry duplicate check
    // is O(1) rather than rebuilding and scanning an array each iteration.
    const seen = new Set(value);
    for (const entry of entries) {
      // Silently ignore exact duplicates — the value is already a chip.
      if (seen.has(entry)) continue;
      // Accepted entries count as existing, so a list containing the same value
      // twice adds it once and the cap applies across the whole paste.
      const existing = [...value, ...accepted];
      const validationError = validate?.(entry, existing) ?? null;
      if (validationError) {
        rejections.push(validationError);
        continue;
      }
      accepted.push(entry);
      seen.add(entry);
    }

    if (accepted.length > 0) onChange([...value, ...accepted]);

    if (entries.length === 1) {
      setError(rejections[0] ?? null);
      // Keep the text only when it was rejected, so it can be edited.
      if (rejections.length === 0) setText("");
      setNotice(null);
      return;
    }

    setText("");
    setError(summariseRejections(rejections));
    setNotice(
      accepted.length > 1
        ? `Added ${accepted.length} ${itemLabelPlural}.`
        : null,
    );
  };

  const removeToken = (index: number) => {
    onChange(value.filter((_, i) => i !== index));
    clearMessages();
  };

  return (
    <SpaceBetween size="xs">
      <SpaceBetween direction="horizontal" size="xs">
        <Input
          value={text}
          onChange={({ detail }) => {
            setText(detail.value);
            if (error || notice) clearMessages();
          }}
          onKeyDown={(event) => {
            if (event.detail.key === "Enter") {
              // Prevent the keypress from submitting an enclosing form.
              event.preventDefault();
              addTokens();
            }
          }}
          placeholder={placeholder}
          ariaLabel={ariaLabel}
          disabled={disabled}
        />
        <Button
          iconName="add-plus"
          disabled={disabled || text.trim().length === 0}
          onClick={addTokens}
          ariaLabel={ariaLabel ? `Add ${ariaLabel}` : "Add value"}
        >
          Add
        </Button>
      </SpaceBetween>
      {notice && <Box variant="small">{notice}</Box>}
      {error && (
        <Box variant="small" color="text-status-error">
          {error}
        </Box>
      )}
      {value.length > 0 && (
        <TokenGroup
          items={value.map((v) => ({ label: v, dismissLabel: `Remove ${v}` }))}
          onDismiss={({ detail }) => removeToken(detail.itemIndex)}
        />
      )}
    </SpaceBetween>
  );
};

/**
 * Condense per-entry rejections into one line.
 *
 * A bad paste can fail the same way dozens of times (every entry over the cap
 * gives the same message), so identical reasons are shown once and the tail is
 * counted rather than listed.
 */
function summariseRejections(rejections: string[]): string | null {
  if (rejections.length === 0) return null;
  const unique = [...new Set(rejections)];
  const shown = unique.slice(0, 2).join(" ");
  const remaining = unique.length - 2;
  const skipped = `${rejections.length} skipped.`;
  return remaining > 0
    ? `${skipped} ${shown} And ${remaining} other problem${remaining === 1 ? "" : "s"}.`
    : `${skipped} ${shown}`;
}
