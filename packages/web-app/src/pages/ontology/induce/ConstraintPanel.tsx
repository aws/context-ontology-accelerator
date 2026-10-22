// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * ConstraintPanel — reviewable SHACL constraint config.
 *
 * Three sections:
 *   1. Auto-generated (from DB constraints) — checkboxes, always present
 *   2. Domain constraints (LLM-inferred) — on-demand via "Generate" button
 *   3. Custom rules — NL input + raw SHACL paste for power users
 */
import React, { useState } from "react";
import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Input from "@cloudscape-design/components/input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Textarea from "@cloudscape-design/components/textarea";
import { ButtonWithHint } from "@components/ButtonWithHint";
import { InfoPopover } from "@components/InfoPopover";
import { downloadTextFile } from "@utils/download";
import { useAppI18n, type AppTranslate } from "../../../i18n/LocaleProvider";

interface PropertyConstraint {
  property_path: string;
  property_name: string;
  constraint_type: string;
  enabled: boolean;
  params: Record<string, unknown>;
  source: string;
  description: string;
}

interface ClassConstraints {
  class_uri: string;
  class_name: string;
  constraints: PropertyConstraint[];
}

interface ConstraintConfig {
  classes: ClassConstraints[];
}

function sourceBadge(source: string, t: AppTranslate) {
  switch (source) {
    case "db_constraint":
      return <Badge color="blue">DB</Badge>;
    case "llm_inferred":
      return <Badge color="green">LLM</Badge>;
    case "user_added":
      return <Badge color="grey">{t("custom")}</Badge>;
    default:
      return <Badge>{source}</Badge>;
  }
}

function typeBadge(type: string, t: AppTranslate) {
  switch (type) {
    case "required":
      return <Badge color="severity-medium">{t("required")}</Badge>;
    case "unique":
      return <Badge color="blue">{t("unique")}</Badge>;
    case "positive":
      return <Badge color="green">{t("positive")}</Badge>;
    case "reference":
      return <Badge color="blue">{t("reference")}</Badge>;
    case "datatype":
      return <Badge color="grey">{t("datatype")}</Badge>;
    default:
      return <Badge>{type}</Badge>;
  }
}

interface ValidationResult {
  status: string;
  passed?: boolean;
  findings?: Array<{ severity: string; message: string; validator?: string }>;
}

export function ConstraintPanel({
  config,
  onConfigChange,
  onInfer,
  onValidate,
  inferring,
  validating,
  validationResult,
  onDismissValidation,
  customTurtle,
  onCustomTurtleChange,
  compiledShapes,
  proposalName,
  isReadOnly,
}: {
  config: ConstraintConfig | null;
  onConfigChange: (config: ConstraintConfig) => void;
  onInfer: () => void;
  onValidate: (config: ConstraintConfig, customTurtle: string) => void;
  inferring?: boolean;
  validating?: boolean;
  validationResult?: ValidationResult | null;
  onDismissValidation?: () => void;
  customTurtle: string;
  onCustomTurtleChange: (value: string) => void;
  // Compiled SHACL shapes (Turtle) cached after the last Validate/compile.
  // ``null`` until the user has run Validate at least once — the Export button
  // stays disabled while it is null.
  compiledShapes?: string | null;
  // Proposal label, used to name the exported ``.shapes.ttl`` file.
  proposalName?: string;
  // Accepted/rejected/cancelled proposals are read-only: constraint editing,
  // custom-rule add, and custom-SHACL input are disabled (mirrors the other
  // editable panels in ProposalDetail).
  isReadOnly?: boolean;
}) {
  const { t } = useAppI18n();
  const [newRule, setNewRule] = useState("");

  function toggleConstraint(
    classIdx: number,
    constraintIdx: number,
    enabled: boolean,
  ) {
    if (!config) return;
    const updated = {
      ...config,
      classes: config.classes.map((cls, ci) =>
        ci === classIdx
          ? {
              ...cls,
              constraints: cls.constraints.map((c, pi) =>
                pi === constraintIdx ? { ...c, enabled } : c,
              ),
            }
          : cls,
      ),
    };
    onConfigChange(updated);
  }

  function addCustomRule() {
    if (!newRule.trim() || !config) return;
    const updated = {
      ...config,
      classes:
        config.classes.length > 0
          ? config.classes.map((cls, i) =>
              i === 0
                ? {
                    ...cls,
                    constraints: [
                      ...cls.constraints,
                      {
                        property_path: "",
                        property_name: "",
                        constraint_type: "custom",
                        enabled: true,
                        params: {},
                        source: "user_added",
                        description: newRule.trim(),
                      },
                    ],
                  }
                : cls,
            )
          : [
              {
                class_uri: "",
                class_name: "Custom",
                constraints: [
                  {
                    property_path: "",
                    property_name: "",
                    constraint_type: "custom",
                    enabled: true,
                    params: {},
                    source: "user_added",
                    description: newRule.trim(),
                  },
                ],
              },
            ],
    };
    onConfigChange(updated);
    setNewRule("");
  }

  const totalEnabled =
    config?.classes.reduce(
      (acc, cls) => acc + cls.constraints.filter((c) => c.enabled).length,
      0,
    ) ?? 0;

  return (
    <ExpandableSection
      variant="container"
      defaultExpanded={false}
      headerText={t("SHACL Constraints")}
      headerInfo={
        <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
          <Badge color="blue">Beta</Badge>
          <InfoPopover
            header={t("SHACL constraints")}
            ariaLabel={t("About SHACL constraints")}
          >
            <SpaceBetween size="xs">
              <Box variant="p">
                {t(
                  "Formal data-quality rules for this ontology (required fields, uniqueness, value ranges, references).",
                )}
              </Box>
              <Box variant="p">
                {t(
                  "Context Ontology Accelerator pre-fills them from your database schema, and Infer adds domain rules using an AI model.",
                )}
              </Box>
              <Box variant="p">
                {t(
                  "Validate compiles the enabled rules and checks the proposal against them; violations are reported before you accept.",
                )}
              </Box>
            </SpaceBetween>
          </InfoPopover>
        </SpaceBetween>
      }
      headerCounter={
        config ? t("({count} active)", { count: totalEnabled }) : undefined
      }
      headerActions={
        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
          <ButtonWithHint
            hint={t(
              "Uses an AI model to suggest domain rules the schema can't capture (value ranges, date ordering, allowed status values). Suggestions are tagged LLM to keep or remove.",
            )}
            onClick={onInfer}
            loading={inferring}
            disabled={isReadOnly}
            iconName="gen-ai"
          >
            {t("Infer constraints")}
          </ButtonWithHint>
          <ButtonWithHint
            hint={t(
              "Compiles the enabled constraints into SHACL shapes and checks the proposal against them, reporting violations below. Also unlocks Export SHACL.",
            )}
            variant="primary"
            onClick={() => config && onValidate(config, customTurtle)}
            loading={validating}
            disabled={!config || totalEnabled === 0}
          >
            {t("Validate")}
          </ButtonWithHint>
          <ButtonWithHint
            hint={t(
              "Downloads the compiled constraints as a standard SHACL Turtle file you can version, share, or run in any SHACL validator. Enabled after you run Validate.",
            )}
            iconName="download"
            onClick={() => {
              if (!compiledShapes) return;
              const safeName = (proposalName || "shapes")
                .replace(/[^A-Za-z0-9._-]/g, "_")
                .slice(0, 80);
              downloadTextFile(
                `${safeName || "shapes"}.shapes.ttl`,
                compiledShapes,
              );
            }}
            disabled={!compiledShapes}
          >
            {t("Export SHACL")}
          </ButtonWithHint>
        </SpaceBetween>
      }
    >
      <SpaceBetween size="m">
        {/* Progress indicator for the async infer-constraints job — kept at the
            TOP of the panel body (directly under the "Infer constraints" button)
            so the user sees it immediately, not scrolled off the bottom. */}
        {inferring && (
          <StatusIndicator type="loading">
            {t("Inferring domain constraints from ontology...")}
          </StatusIndicator>
        )}

        {validationResult &&
          validationResult.status === "completed" &&
          (() => {
            // This panel validates the SHACL CONSTRAINTS specifically, so its
            // pass/fail must reflect the SHACL findings shown below — NOT the
            // run-level `passed`, which also folds in connectivity / reasoner /
            // consistency findings from the same tier1 job (those are hidden
            // here, so using run-level `passed` produced a "failed" header
            // above an "all constraints satisfied" body).
            const shaclFindings =
              validationResult.findings?.filter(
                (f) => f.validator === "shacl",
              ) ?? [];
            const shaclPassed = !shaclFindings.some(
              (f) => f.severity === "error",
            );
            return (
              <Alert
                type={shaclPassed ? "success" : "error"}
                header={
                  shaclPassed
                    ? t("SHACL validation passed")
                    : t("SHACL validation failed")
                }
                dismissible
                onDismiss={onDismissValidation}
              >
                {shaclFindings.map((f, i) => (
                  <Box key={i} variant="small">
                    <Badge
                      color={
                        f.severity === "error"
                          ? "red"
                          : f.severity === "info"
                            ? "blue"
                            : "severity-medium"
                      }
                    >
                      {f.severity}
                    </Badge>{" "}
                    {f.message}
                  </Box>
                ))}
              </Alert>
            );
          })()}

        {!config && (
          <Box variant="small" color="text-status-inactive">
            {t(
              "No constraint config available. Run a new induction to auto-generate constraints from database schema.",
            )}
          </Box>
        )}

        {config && config.classes.length > 0 && (
          <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
            <Box variant="small" color="text-status-inactive">
              {t("Per class: enable the rules to enforce.")}
            </Box>
            <InfoPopover
              header={t("Badges legend")}
              ariaLabel={t("About constraint sources and types")}
            >
              <SpaceBetween size="xs">
                <Box variant="p">{t("Sources")}</Box>
                <Box variant="p">
                  {t(
                    "DB: from your database schema (NOT NULL, UNIQUE, keys, column types).",
                  )}
                </Box>
                <Box variant="p">
                  {t(
                    "LLM: a domain rule suggested by Infer. Review before trusting.",
                  )}
                </Box>
                <Box variant="p">
                  {t(
                    "Custom: a rule you added (natural language or pasted SHACL).",
                  )}
                </Box>
                <Box variant="p">{t("Types")}</Box>
                <Box variant="p">
                  {t(
                    "required: field must have a value. unique: at most one value per record. reference: must point to a related record. datatype: pins the value's type. positive: value above a minimum.",
                  )}
                </Box>
              </SpaceBetween>
            </InfoPopover>
          </SpaceBetween>
        )}

        {config &&
          config.classes.map((cls, classIdx) => (
            <ExpandableSection
              key={cls.class_uri || cls.class_name}
              headerText={`${cls.class_name} (${cls.constraints.filter((c) => c.enabled).length}/${cls.constraints.length})`}
              defaultExpanded={classIdx < 3}
              variant="footer"
            >
              <SpaceBetween size="xs">
                {cls.constraints.map((constraint, constraintIdx) => (
                  <div
                    key={`${constraint.property_name}-${constraint.constraint_type}-${constraintIdx}`}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      opacity: constraint.enabled ? 1 : 0.5,
                    }}
                  >
                    <Checkbox
                      checked={constraint.enabled}
                      disabled={isReadOnly}
                      ariaLabel={t("Enable constraint {name}", {
                        name:
                          constraint.property_name ||
                          constraint.constraint_type,
                      })}
                      onChange={({ detail }) =>
                        toggleConstraint(
                          classIdx,
                          constraintIdx,
                          detail.checked,
                        )
                      }
                    />
                    <Box variant="small" display="inline" fontWeight="bold">
                      {constraint.property_name || "—"}
                    </Box>
                    {typeBadge(constraint.constraint_type, t)}
                    {sourceBadge(constraint.source, t)}
                    <Box
                      variant="small"
                      color="text-status-inactive"
                      display="inline"
                    >
                      {constraint.description}
                    </Box>
                  </div>
                ))}
              </SpaceBetween>
            </ExpandableSection>
          ))}

        <ExpandableSection headerText={t("Add custom rule")} variant="footer">
          <SpaceBetween size="s">
            <Box variant="small" color="text-status-inactive">
              {t(
                'Natural-language rules are recorded for reviewer context but are not automatically compiled into SHACL. To enforce a rule, express it as a structured constraint above or paste SHACL in "Custom SHACL (advanced)".',
              )}
            </Box>
            <div style={{ display: "flex", gap: 8 }}>
              <div style={{ flex: 1 }}>
                <Input
                  value={newRule}
                  disabled={isReadOnly}
                  ariaLabel={t("Natural-language constraint rule")}
                  onChange={({ detail }) => setNewRule(detail.value)}
                  placeholder={t(
                    "e.g., Every policy must have a premium amount greater than zero",
                  )}
                />
              </div>
              <Button
                onClick={addCustomRule}
                disabled={isReadOnly || !newRule.trim()}
              >
                {t("Add")}
              </Button>
            </div>
          </SpaceBetween>
        </ExpandableSection>

        <ExpandableSection
          headerText={t("Custom SHACL (advanced)")}
          variant="footer"
        >
          <SpaceBetween size="s">
            <Box variant="small" color="text-status-inactive">
              {t(
                "Paste additional SHACL shapes in Turtle format. These are appended verbatim to the compiled constraints.",
              )}
            </Box>
            <Textarea
              value={customTurtle}
              disabled={isReadOnly}
              ariaLabel={t("Custom SHACL shapes (Turtle)")}
              onChange={({ detail }) => onCustomTurtleChange(detail.value)}
              placeholder="@prefix sh: <http://www.w3.org/ns/shacl#> ."
              rows={5}
            />
          </SpaceBetween>
        </ExpandableSection>
      </SpaceBetween>
    </ExpandableSection>
  );
}
