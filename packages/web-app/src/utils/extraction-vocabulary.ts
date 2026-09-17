// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Helpers for the extraction-vocabulary form fields (entity classes, topics).
 *
 * Labels and topics are sent to the extractor **verbatim**. graphrag-toolkit joins
 * the preferred list straight into the prompt and imposes no format of its own, so
 * the user's spelling is the user's choice and nothing here rewrites it.
 *
 * The toolkit does, however, title-case whatever the LLM *emits*
 * (`format_classification`, `topic_utils.py:112`), so an entity class can land in
 * the graph spelled differently from the label that was configured:
 *
 * `"Dress"`           -> `"Dress"`             (unchanged)
 * `"DRESS"`           -> `"Dress"`
 * `"Style archetype"` -> `"Style Archetype"`
 * `"StyleArchetype"`  -> `"Stylearchetype"`    (word boundary lost)
 *
 * `predictStoredForm` exists to show that difference in the form rather than let
 * the configured vocabulary and the graph diverge unnoticed. Topics are not
 * title-cased, so they are unaffected.
 */

/** Max length accepted by the API for one entity-class label. */
export const MAX_CLASSIFICATION_LENGTH = 128;
/** Max length accepted by the API for one topic name. */
export const MAX_TOPIC_LENGTH = 256;
/**
 * Hard cap on entries in either list, enforced by the API.
 *
 * Mirrors `MAX_VOCABULARY_ENTRIES` in `libs/common/src/coa_common/constants.py`.
 * Enforced here too so the user is stopped at the point of entry rather than
 * discovering it as a 400 after filling in the whole form.
 */
export const MAX_VOCABULARY_ENTRIES = 100;

/**
 * Apply the same transform the ingest parser applies to an entity-class label:
 * `s.replace('_', ' ').title()` in Python.
 *
 * Python's `str.title()` capitalises the first letter of every run of letters,
 * not just after spaces — so `"t-shirt"` becomes `"T-Shirt"` and `"o'brien"`
 * becomes `"O'Brien"`. Splitting on spaces alone would give `"T-shirt"` and
 * disagree with the server, which is the one thing this helper exists to predict.
 */
export function toParserForm(raw: string): string {
  return raw
    .replace(/_/g, " ")
    .toLowerCase()
    .replace(
      /(^|[^a-z])([a-z])/g,
      (_match, prefix: string, letter: string) => prefix + letter.toUpperCase(),
    );
}

/**
 * Predict the class name a label will end up as in the graph.
 *
 * Labels are sent to the extractor **verbatim** — graphrag-toolkit imposes no
 * format on the preferred list, so we do not rewrite the user's spelling. But the
 * toolkit *does* title-case whatever the LLM emits
 * (`format_classification`, `topic_utils.py:112`), so the stored class can differ
 * from what was typed. Returning that difference lets the UI show it instead of
 * silently letting the two diverge.
 *
 * Returns `null` when the label round-trips unchanged.
 */
export function predictStoredForm(raw: string): string | null {
  const trimmed = raw.trim();
  const stored = toParserForm(trimmed);
  return stored === trimmed ? null : stored;
}

/**
 * Labels whose stored class name will differ from what was typed, for a
 * non-blocking notice. Order is preserved so the message reads predictably.
 */
export function labelsRewrittenOnIngest(
  labels: string[],
): { typed: string; stored: string }[] {
  return labels.flatMap((typed) => {
    const stored = predictStoredForm(typed);
    return stored === null ? [] : [{ typed, stored }];
  });
}

/**
 * Apply the transforms the ingest parser applies to a TOPIC name: `format_value`
 * (underscore to space) then `strip_full_stop`, at `topic_utils.py:79`.
 *
 * Unlike an entity class a topic is NOT title-cased, so the user's capitalisation
 * survives — but these two transforms still apply, which is why a topic can also
 * land in the graph spelled differently from what was configured.
 */
export function toTopicStoredForm(raw: string): string {
  const spaced = raw.replace(/_/g, " ").trim();
  return spaced.endsWith(".") ? spaced.slice(0, -1) : spaced;
}

/**
 * Predict the topic name that will be recorded, or `null` when it round-trips
 * unchanged. Mirrors {@link predictStoredForm} for the topic axis.
 */
export function predictStoredTopicForm(raw: string): string | null {
  const trimmed = raw.trim();
  const stored = toTopicStoredForm(trimmed);
  return stored === trimmed ? null : stored;
}

/** Topics whose recorded name will differ from what was typed. */
export function topicsRewrittenOnIngest(
  topics: string[],
): { typed: string; stored: string }[] {
  return topics.flatMap((typed) => {
    const stored = predictStoredTopicForm(typed);
    return stored === null ? [] : [{ typed, stored }];
  });
}

/**
 * Validator for entity-class labels, shaped for `TokenInput.validate`.
 *
 * Rejects only what the API rejects: empty, over-length, duplicate, or past the
 * list cap. Spelling is deliberately not policed — see the module comment.
 */
export function validateClassification(
  candidate: string,
  existing: string[] = [],
): string | null {
  const trimmed = candidate.trim();
  if (trimmed.length === 0) return "Enter a label.";
  if (trimmed.length > MAX_CLASSIFICATION_LENGTH) {
    return `Labels must be ${MAX_CLASSIFICATION_LENGTH} characters or fewer.`;
  }
  if (existing.length >= MAX_VOCABULARY_ENTRIES) {
    return `You can add at most ${MAX_VOCABULARY_ENTRIES} labels.`;
  }
  // The API compares case-insensitively, because "Dress" and "DRESS" are stored
  // as the same class. Match that here so the UI does not accept a pair the API
  // will reject.
  if (existing.some((e) => e.toLowerCase() === trimmed.toLowerCase())) {
    return `"${trimmed}" duplicates a label you have already added.`;
  }
  return null;
}

/** Validator for topic names. Topics are phrases, so only length is enforced. */
export function validateTopic(
  candidate: string,
  existing: string[] = [],
): string | null {
  const trimmed = candidate.trim();
  if (trimmed.length === 0) return "Enter a topic name.";
  if (trimmed.length > MAX_TOPIC_LENGTH) {
    return `Topic names must be ${MAX_TOPIC_LENGTH} characters or fewer.`;
  }
  if (existing.length >= MAX_VOCABULARY_ENTRIES) {
    return `You can add at most ${MAX_VOCABULARY_ENTRIES} topics.`;
  }
  if (existing.some((e) => e.toLowerCase() === trimmed.toLowerCase())) {
    return `"${trimmed}" duplicates a topic you have already added.`;
  }
  return null;
}

/**
 * Number of labels beyond which prompt adherence measurably degrades. Advisory
 * only — the API imposes no list-length cap.
 */
export const RECOMMENDED_MAX_LABELS = 40;
