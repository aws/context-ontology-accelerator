// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table, { type TableProps } from "@cloudscape-design/components/table";

import {
  addAlternativeLabel,
  addPreferredLabel,
  labelKey,
  makePreferredLabel,
  parseVocabulary,
  removeAlternativeLabel,
  replacePreferredLabel,
  safeVocabularyDisplayText,
  VocabularyEditError,
  type VocabularyConcept,
  type VocabularyLabel,
} from "./vocabulary";
import { useAppI18n, type AppTranslate } from "../../../i18n/LocaleProvider";
import type { AppMessage } from "../../../i18n/appMessages";
import { isSubmitEnter, type EnterKeyDetail } from "../../../utils/keyboard";

const LANG_STRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString";
const XSD_STRING = "http://www.w3.org/2001/XMLSchema#string";

type ReviewAction =
  | {
      kind: "edit-preferred";
      concept: VocabularyConcept;
      label: VocabularyLabel;
      baseTurtle: string;
    }
  | { kind: "add-preferred"; concept: VocabularyConcept; baseTurtle: string }
  | { kind: "add-alternative"; concept: VocabularyConcept; baseTurtle: string };

interface Props {
  turtle: string;
  /** Absolute IRIs of the terms to review, in display order. */
  termIris: readonly string[];
  isReadOnly: boolean;
  onChange: (updatedTurtle: string) => void;
}

interface ParsedVocabulary {
  concepts: VocabularyConcept[];
  error: string | null;
}

function compactIri(iri: string): string {
  const hash = iri.lastIndexOf("#");
  const slash = iri.lastIndexOf("/");
  return safeVocabularyDisplayText(iri.slice(Math.max(hash, slash) + 1) || iri);
}

function labelText(label: VocabularyLabel): string {
  const value = safeVocabularyDisplayText(label.value);
  return label.language ? `${value} @${label.language}` : value;
}

function termName(concept: VocabularyConcept): string {
  return (
    safeVocabularyDisplayText(concept.preferredLabels[0]?.value ?? "") ||
    compactIri(concept.id)
  );
}

function draftLabel(
  value: string,
  language: string,
  untaggedDatatype = XSD_STRING,
): VocabularyLabel {
  const trimmedLanguage = language.trim();
  return {
    value: value.trim(),
    language: trimmedLanguage,
    datatype: trimmedLanguage ? LANG_STRING : untaggedDatatype,
  };
}

function describeError(
  error: unknown,
  t: AppTranslate,
  fallback: AppMessage,
): string {
  return error instanceof VocabularyEditError
    ? t(error.template, error.values)
    : t(fallback);
}

export function VocabularyReviewPanel({
  turtle,
  termIris,
  isReadOnly,
  onChange,
}: Props) {
  const { t } = useAppI18n();
  const vocabulary = useMemo((): ParsedVocabulary => {
    try {
      return { concepts: parseVocabulary(turtle), error: null };
    } catch (error) {
      return {
        concepts: [],
        error: describeError(error, t, "The vocabulary could not be parsed"),
      };
    }
  }, [turtle, t]);
  const concepts = useMemo(() => {
    const byId = new Map(
      vocabulary.concepts.map((concept) => [concept.id, concept]),
    );
    return [...new Set(termIris)].flatMap((iri) => {
      const concept = byId.get(iri);
      return concept ? [concept] : [];
    });
  }, [vocabulary, termIris]);
  const [action, setAction] = useState<ReviewAction | null>(null);
  const [draftValue, setDraftValue] = useState("");
  const [draftLanguage, setDraftLanguage] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);

  const begin = (next: ReviewAction) => {
    setAction(next);
    setActionError(null);
    if (next.kind === "edit-preferred") {
      setDraftValue(next.label.value);
      setDraftLanguage(next.label.language);
    } else {
      setDraftValue("");
      setDraftLanguage("");
    }
  };

  const close = () => {
    setAction(null);
    setActionError(null);
  };

  const applyUpdate = (update: () => string) => {
    setActionError(null);
    try {
      const updated = update();
      if (updated !== turtle) onChange(updated);
      setAction(null);
    } catch (error) {
      setActionError(describeError(error, t, "The edit could not be applied"));
    }
  };

  const stale = action !== null && action.baseTurtle !== turtle;

  const columns: TableProps.ColumnDefinition<VocabularyConcept>[] = [
    {
      id: "concept",
      header: t("Concept"),
      isRowHeader: true,
      cell: (concept) => {
        const kind = concept.kind
          ? safeVocabularyDisplayText(concept.kind)
          : t("Resource");
        return (
          <SpaceBetween size="xxs">
            <Box variant="strong">{termName(concept)}</Box>
            <Box variant="small" color="text-status-inactive">
              {kind} · {compactIri(concept.id)}
            </Box>
          </SpaceBetween>
        );
      },
    },
    {
      id: "preferred",
      header: t("Preferred labels"),
      cell: (concept) => (
        <SpaceBetween size="xxs">
          {concept.preferredLabels.map((label) => (
            <SpaceBetween
              key={labelKey(label)}
              direction="horizontal"
              size="xs"
              alignItems="center"
            >
              <span>{labelText(label)}</span>
              {!isReadOnly && (
                <Button
                  variant="inline-link"
                  ariaLabel={t("Edit preferred label {label}", {
                    label: labelText(label),
                  })}
                  onClick={() =>
                    begin({
                      kind: "edit-preferred",
                      concept,
                      label,
                      baseTurtle: turtle,
                    })
                  }
                >
                  {t("Edit")}
                </Button>
              )}
            </SpaceBetween>
          ))}
          {!isReadOnly && (
            <Button
              variant="inline-link"
              ariaLabel={t("Add language to {term}", {
                term: termName(concept),
              })}
              onClick={() =>
                begin({ kind: "add-preferred", concept, baseTurtle: turtle })
              }
            >
              {t("Add language")}
            </Button>
          )}
        </SpaceBetween>
      ),
    },
    {
      id: "alternatives",
      header: t("Variants"),
      cell: (concept) => (
        <SpaceBetween size="xs">
          {concept.alternativeLabels.length === 0 && (
            <Box color="text-status-inactive">{t("No variants")}</Box>
          )}
          {concept.alternativeLabels.map((label) => (
            <SpaceBetween
              key={labelKey(label)}
              direction="horizontal"
              size="xs"
              alignItems="center"
            >
              <span>{labelText(label)}</span>
              {!isReadOnly && (
                <SpaceBetween
                  direction="horizontal"
                  size="xs"
                  alignItems="center"
                >
                  <Button
                    variant="inline-link"
                    ariaLabel={t("Make preferred {label}", {
                      label: labelText(label),
                    })}
                    onClick={() =>
                      applyUpdate(() =>
                        makePreferredLabel(turtle, concept.id, label),
                      )
                    }
                  >
                    {t("Make preferred")}
                  </Button>
                  <Button
                    variant="inline-link"
                    ariaLabel={t("Reject variant {label}", {
                      label: labelText(label),
                    })}
                    onClick={() =>
                      applyUpdate(() =>
                        removeAlternativeLabel(turtle, concept.id, label),
                      )
                    }
                  >
                    {t("Reject")}
                  </Button>
                </SpaceBetween>
              )}
            </SpaceBetween>
          ))}
          {!isReadOnly && (
            <Button
              variant="inline-link"
              ariaLabel={t("Add variant to {term}", {
                term: termName(concept),
              })}
              onClick={() =>
                begin({ kind: "add-alternative", concept, baseTurtle: turtle })
              }
            >
              {t("Add variant")}
            </Button>
          )}
        </SpaceBetween>
      ),
    },
  ];

  const modalHeader = !action
    ? ""
    : action.kind === "edit-preferred"
      ? t("Edit preferred label {label}", { label: labelText(action.label) })
      : action.kind === "add-preferred"
        ? t("Add language to {term}", { term: termName(action.concept) })
        : t("Add variant to {term}", { term: termName(action.concept) });

  const canApply = !stale && draftValue.trim().length > 0;

  const submitOnEnter = (event: CustomEvent<EnterKeyDetail>) => {
    if (isSubmitEnter(event.detail)) submit();
  };

  const submit = () => {
    // The dialog shows the stale warning and disables Apply; never apply an
    // edit that was opened against an older Turtle. Enter goes through the
    // same check as the button.
    if (!action || !canApply) return;
    const draft = draftLabel(
      draftValue,
      draftLanguage,
      action.kind === "edit-preferred" ? action.label.datatype : undefined,
    );
    switch (action.kind) {
      case "edit-preferred":
        applyUpdate(() =>
          replacePreferredLabel(turtle, action.concept.id, action.label, draft),
        );
        break;
      case "add-preferred":
        applyUpdate(() => addPreferredLabel(turtle, action.concept.id, draft));
        break;
      case "add-alternative":
        applyUpdate(() =>
          addAlternativeLabel(turtle, action.concept.id, draft),
        );
        break;
    }
  };

  if (concepts.length === 0 && !vocabulary.error) return null;

  return (
    <>
      <ExpandableSection
        defaultExpanded
        headerText={t("Business vocabulary ({count})", {
          count: concepts.length,
        })}
      >
        <SpaceBetween size="s">
          <Box variant="small" color="text-body-secondary">
            {t(
              "Edits apply to this proposal only. A re-induction builds its labels and variants from the source metadata again, so add lasting synonyms there.",
            )}
          </Box>
          {vocabulary.error && (
            <Alert
              type="warning"
              header={t("Vocabulary review is unavailable")}
            >
              {vocabulary.error}
            </Alert>
          )}
          {actionError && !action && (
            <Alert
              type="error"
              dismissible
              onDismiss={() => setActionError(null)}
              header={t("The vocabulary edit failed")}
            >
              {actionError}
            </Alert>
          )}
          {!vocabulary.error && (
            <Table
              items={concepts}
              columnDefinitions={columns}
              trackBy="id"
              variant="embedded"
              wrapLines
              ariaLabels={{ tableLabel: t("Business vocabulary") }}
            />
          )}
        </SpaceBetween>
      </ExpandableSection>

      {action && (
        <Modal
          visible
          onDismiss={close}
          header={modalHeader}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={close}>
                  {t("Cancel")}
                </Button>
                <Button variant="primary" disabled={!canApply} onClick={submit}>
                  {t("Apply")}
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="s">
            {stale && (
              <Alert type="warning">
                {t(
                  "The proposal changed while this dialog was open. Close it and try again.",
                )}
              </Alert>
            )}
            {actionError && (
              <Alert type="error" header={t("The vocabulary edit failed")}>
                {actionError}
              </Alert>
            )}
            <FormField label={t("Label")}>
              <Input
                value={draftValue}
                onChange={({ detail }) => setDraftValue(detail.value)}
                onKeyDown={submitOnEnter}
              />
            </FormField>
            <FormField
              label={t("Language tag")}
              description={t(
                "Use a BCP 47 tag such as ja or en. Leave blank for an untagged label.",
              )}
            >
              <Input
                value={draftLanguage}
                onChange={({ detail }) => setDraftLanguage(detail.value)}
                onKeyDown={submitOnEnter}
              />
            </FormField>
          </SpaceBetween>
        </Modal>
      )}
    </>
  );
}
