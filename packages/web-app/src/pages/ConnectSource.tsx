// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useCallback, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import Wizard from "@cloudscape-design/components/wizard";
import type { WizardProps } from "@cloudscape-design/components/wizard";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Checkbox from "@cloudscape-design/components/checkbox";
import RadioGroup from "@cloudscape-design/components/radio-group";
import Container from "@cloudscape-design/components/container";
import CopyToClipboard from "@cloudscape-design/components/copy-to-clipboard";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import FileUpload from "@cloudscape-design/components/file-upload";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Select from "@cloudscape-design/components/select";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Tabs from "@cloudscape-design/components/tabs";
import Tiles from "@cloudscape-design/components/tiles";
import Toggle from "@cloudscape-design/components/toggle";
import { TokenInput } from "@components/TokenInput/TokenInput";
import {
  MAX_CLASSIFICATION_LENGTH,
  MAX_TOPIC_LENGTH,
  MAX_VOCABULARY_ENTRIES,
  labelsRewrittenOnIngest,
  topicsRewrittenOnIngest,
  RECOMMENDED_MAX_LABELS,
  validateClassification,
  validateTopic,
} from "@utils/extraction-vocabulary";
import {
  useCreateSource,
  useGetNamespace,
  useGetSourceUploadUrls,
} from "@api-hooks";
import {
  ControlPlaneServiceServiceException,
  SourceType,
} from "@coa/control-plane-client";

/** Read-only display of the ExternalId a cross-account role's trust policy must pin.
 *
 * Deliberately not an input. The platform derives this value from the namespace
 * and refuses to assume a role without presenting it, so letting the user choose
 * it would defeat the binding it exists to provide — it is what stops a role
 * onboarded for one namespace from being read by another.
 */
function CrossAccountExternalIdField({
  externalId,
  isLoading,
}: {
  readonly externalId: string | undefined;
  readonly isLoading: boolean;
}): React.JSX.Element {
  return (
    <FormField
      label="External ID (add to the role's trust policy)"
      description="Context Ontology Accelerator presents this value when assuming the role above. Add it as an sts:ExternalId condition on that role's trust policy — without the condition the role is assumable on behalf of any namespace in this deployment."
    >
      {externalId ? (
        <CopyToClipboard
          copyButtonAriaLabel="Copy External ID"
          copyButtonText="Copy"
          copySuccessText="External ID copied"
          copyErrorText="Failed to copy External ID"
          textToCopy={externalId}
          variant="inline"
        />
      ) : (
        <Box color="text-status-inactive">
          {isLoading
            ? "Loading…"
            : "Unavailable — reload the page to retrieve it."}
        </Box>
      )}
    </FormField>
  );
}

/** Extract a human-readable message from any thrown value.
 *  Smithy sets `message = "UnknownError"` for unmodeled error responses;
 *  in that case we fall back to the raw response body's `error` field. */
function extractErrorMessage(
  err: unknown,
  fallback = "An unexpected error occurred.",
): string {
  if (err instanceof ControlPlaneServiceServiceException) {
    // For unmodeled errors the Smithy client sets message === name === "UnknownError".
    if (err.message && err.message !== "UnknownError") return err.message;

    // The Smithy REST-JSON deserializer copies unknown fields from the parsed
    // response body onto the exception object directly. Our API returns
    // {"error": "..."} so check for that field first.
    const anyErr = err as unknown as Record<string, unknown>;
    if (typeof anyErr["error"] === "string") return anyErr["error"];

    // Try to pull the `error` field out of the raw HTTP response body.
    const body = (err as unknown as { $response?: { body?: unknown } })
      .$response?.body;
    if (typeof body === "string") {
      try {
        const parsed = JSON.parse(body) as Record<string, unknown>;
        if (typeof parsed.error === "string") return parsed.error;
        if (typeof parsed.message === "string") return parsed.message;
      } catch {
        // body wasn't JSON — ignore
      }
    }
    return fallback;
  }
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}
import { SUPPORTED_UPLOAD_CONTENT_TYPES, MAX_UPLOAD_FILES } from "@coa/shared";
import { S3_ARN_RE, validateS3Prefix } from "@utils/helpers";

// ── Source type options ───────────────────────────────────────────────────────

// Four-way selection shown in step 1
type SourceKind =
  | "GLUE_DATABASE"
  | "JDBC_DATABASE"
  | "CUSTOM_CONNECTOR"
  | "DOCUMENTS";

/** Narrow a Tiles selection to a SourceKind. Cloudscape hands back a plain
 *  string, and a guard keeps that boundary honest without a cast. */
function isSourceKind(value: string): value is SourceKind {
  return (
    value === "GLUE_DATABASE" ||
    value === "JDBC_DATABASE" ||
    value === "CUSTOM_CONNECTOR" ||
    value === "DOCUMENTS"
  );
}

// Labels for the step-1 tiles, reused by the review step's summary.
const SOURCE_KIND_LABELS: Record<SourceKind, string> = {
  GLUE_DATABASE: "Glue database",
  JDBC_DATABASE: "JDBC database",
  CUSTOM_CONNECTOR: "Custom connector",
  DOCUMENTS: "Documents",
};

// ── Constants ─────────────────────────────────────────────────────────────────

// Every engine the backend has a dialect for.
type DatabaseEngine =
  | "POSTGRESQL"
  | "MYSQL"
  | "REDSHIFT"
  | "ORACLE"
  | "SQLSERVER"
  | "SNOWFLAKE";

// Execution engine for a Glue/Iceberg source at serve time. ATHENA is
// the default; REDSHIFT routes queries through Redshift Serverless
// (awsdatacatalog auto-mount). Distinct from DatabaseEngine (which types a
// native JDBC *source*), so the Glue form can never pick a non-Glue engine.
type GlueExecutionEngine = "ATHENA" | "REDSHIFT";

const GLUE_EXECUTION_ENGINE_OPTIONS: {
  value: GlueExecutionEngine;
  label: string;
  description: string;
}[] = [
  {
    value: "ATHENA",
    label: "Athena (default)",
    description: "Query Glue/Iceberg tables via Amazon Athena.",
  },
  {
    value: "REDSHIFT",
    label: "Redshift Serverless",
    description:
      "Query Glue/Iceberg tables via Redshift Serverless (awsdatacatalog auto-mount).",
  },
];

const DEFAULT_PORTS: Record<DatabaseEngine, string> = {
  POSTGRESQL: "5432",
  MYSQL: "3306",
  REDSHIFT: "5439",
  ORACLE: "1521",
  SQLSERVER: "1433",
  SNOWFLAKE: "443",
};

const ENGINE_OPTIONS: Array<{ value: DatabaseEngine; label: string }> = [
  { value: "POSTGRESQL", label: "PostgreSQL" },
  { value: "MYSQL", label: "MySQL" },
  { value: "REDSHIFT", label: "Redshift" },
  { value: "ORACLE", label: "Oracle" },
  { value: "SQLSERVER", label: "SQL Server" },
  { value: "SNOWFLAKE", label: "Snowflake" },
];

// Engine list shown in the JDBC source-kind description. Derived from
// ENGINE_OPTIONS so it can never advertise an engine the picker doesn't offer.
const ENGINE_LABELS = ENGINE_OPTIONS.map((o) => o.label).join(", ");

const EXT_TO_MIME: Record<string, string> = {
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  txt: "text/plain",
  md: "text/markdown",
  markdown: "text/markdown",
};

function resolveContentType(file: File): string {
  if (file.type && SUPPORTED_UPLOAD_CONTENT_TYPES.has(file.type))
    return file.type;
  const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
  return EXT_TO_MIME[ext] ?? file.type;
}

// ── Form model ────────────────────────────────────────────────────────────────

interface Model {
  // Step 1 — source kind (replaces the old two-step category + type)
  sourceKind: SourceKind;
  // Glue database config
  dbName: string;
  glueCatalogId: string;
  glueRegion: string;
  glueDatabaseName: string;
  glueTableFilter: string;
  glueTableExcludeFilter: string;
  glueAthenaDataCatalogName: string;
  glueCrossAccountRoleArn: string;
  // How this Glue/Iceberg source's queries execute at serve time.
  // "ATHENA" (default) or "REDSHIFT" (Redshift Serverless awsdatacatalog
  // auto-mount). NOTE: this is the *execution engine* for a Glue source — it is
  // NOT the same as onboarding a Redshift database (that's a JDBC source).
  glueExecutionEngine: GlueExecutionEngine;
  // Redshift Serverless workgroup — required only when glueExecutionEngine is REDSHIFT.
  glueRedshiftWorkgroup: string;
  // JDBC database config
  jdbcName: string;
  dbEngine: DatabaseEngine | null;
  dbHost: string;
  dbPort: string;
  dbDatabaseName: string;
  dbSchemaFilter: string;
  dbSchemaExcludeFilter: string;
  dbTableFilter: string;
  dbTableExcludeFilter: string;
  dbSecretArn: string;
  dbWarehouse: string;
  dbRole: string;
  jdbcCrossAccountRoleArn: string;
  // Custom Athena Query Federation connector config. The connector is a Lambda
  // the customer authors and deploys in their own account; there is no Glue
  // database and no cross-account role to assume, so this config carries only
  // the function ARNs, the one database to expose, and the table filters.
  customConnectorName: string;
  customConnectorFunctionArn: string;
  customConnectorDatabaseName: string;
  customConnectorTableFilter: string;
  customConnectorTableExcludeFilter: string;
  // Shared database enrichment toggle — when false, the backend skips the
  // AI metadata enrichment step entirely and only persists discovered
  // technical metadata for steward review.
  metadataEnrichmentEnabled: boolean;
  // Documents config (upload tab)
  docUploadName: string;
  docUploadFiles: File[];
  // Documents config (s3 tab)
  docS3Name: string;
  docS3BucketArn: string;
  docS3Prefixes: { id: number; value: string }[];
  docS3RoleArn: string;
  // Shared extraction config
  enableVersioning: boolean;
  deletePrevVersions: boolean;
  // Entity-class vocabulary: "infer" runs a corpus sweep at ingest start,
  // "custom" pins the exact labels below (which disables inference server-side).
  vocabularyMode: VocabularyMode;
  preferredEntityClassifications: string[];
  // Topic vocabulary: "auto" lets the extractor name topics from the chunk text
  // (there is no corpus-inference pass for topics), "custom" supplies a steer.
  topicMode: TopicMode;
  preferredTopics: string[];
}

/** How the entity-class vocabulary is chosen. */
export type VocabularyMode = "infer" | "custom";
/** How topic names are chosen. */
export type TopicMode = "auto" | "custom";

// RadioGroup hands back a plain string, so narrow it with a guard rather than
// asserting — project convention, see .claude/rules/frontend-typescript.md. An
// unrecognised value leaves the current selection alone instead of writing a
// mode the rest of the form cannot interpret.
const isVocabularyMode = (value: string): value is VocabularyMode =>
  value === "infer" || value === "custom";
const isTopicMode = (value: string): value is TopicMode =>
  value === "auto" || value === "custom";

/**
 * Build the `extractionConfig` payload from the form model.
 *
 * Shared by both DOCUMENTS paths (S3 and upload) so they cannot drift. Only
 * sends the vocabulary/topic fields when the user chose to pin them: omitting
 * them preserves today's server-side defaults (corpus inference for classes,
 * free-form topic naming) rather than sending an empty list that reads as an
 * explicit choice.
 */
function buildExtractionConfig(model: Model): {
  enableVersioning: boolean;
  deletePrevVersions: boolean;
  preferredEntityClassifications?: string[];
  inferEntityClassifications?: boolean;
  preferredTopics?: string[];
} {
  const custom = model.vocabularyMode === "custom";
  return {
    enableVersioning: model.enableVersioning,
    deletePrevVersions: model.deletePrevVersions,
    // An explicit list overrides inference server-side, so send the flag too —
    // it makes the intent legible in the stored config and in GetSource.
    ...(custom && model.preferredEntityClassifications.length > 0
      ? {
          preferredEntityClassifications: model.preferredEntityClassifications,
          inferEntityClassifications: false,
        }
      : { inferEntityClassifications: true }),
    ...(model.topicMode === "custom" && model.preferredTopics.length > 0
      ? { preferredTopics: model.preferredTopics }
      : {}),
  };
}

/**
 * Field-level warning for the entity-vocabulary control.
 *
 * Two things are worth flagging without blocking submission: a list long enough to
 * hurt prompt adherence, and labels whose recorded class name will differ from what
 * was typed (extraction title-cases the class it records, so "dress" is recorded as
 * "Dress"). Neither is an error — the labels are used as written — so this is
 * `warningText`, not `errorText`.
 */
function vocabularyWarning(labels: string[]): string | undefined {
  const rewritten = labelsRewrittenOnIngest(labels);
  const parts: string[] = [];
  if (labels.length > RECOMMENDED_MAX_LABELS) {
    parts.push(
      `${labels.length} classes. Long lists measurably reduce how closely the extractor follows them.`,
    );
  }
  if (rewritten.length > 0) {
    // List every affected label, not a truncated sample: this warning exists to
    // tell users which of their labels will be renamed on ingest, and hiding some
    // behind "and N more" defeats it. The set is naturally small — only labels
    // whose recorded form differs from what was typed.
    const shown = rewritten
      .map((r) => `“${r.typed}” as “${r.stored}”`)
      .join(", ");
    parts.push(`Extraction will record ${shown}.`);
  }
  return parts.length > 0 ? parts.join(" ") : undefined;
}

/**
 * Same idea as {@link vocabularyWarning}, for topics. Topics keep their
 * capitalisation, but an underscore still becomes a space and a trailing full stop
 * is dropped, so a supplied name can still be recorded differently. Surfacing the
 * specific names beats a general sentence nobody reads.
 */
function topicWarning(topics: string[]): string | undefined {
  const rewritten = topicsRewrittenOnIngest(topics);
  if (rewritten.length === 0) return undefined;
  const shown = rewritten
    .slice(0, 3)
    .map((r) => `“${r.typed}” as “${r.stored}”`)
    .join(", ");
  const more = rewritten.length > 3 ? ` and ${rewritten.length - 3} more` : "";
  return `Extraction will record ${shown}${more}.`;
}

const initialModel: Model = {
  sourceKind: "GLUE_DATABASE",
  dbName: "",
  glueCatalogId: "",
  glueRegion: "us-east-1",
  glueDatabaseName: "",
  glueTableFilter: "",
  glueTableExcludeFilter: "",
  glueAthenaDataCatalogName: "",
  glueCrossAccountRoleArn: "",
  glueExecutionEngine: "ATHENA",
  glueRedshiftWorkgroup: "",
  jdbcName: "",
  dbEngine: null,
  dbHost: "",
  dbPort: "5432",
  dbDatabaseName: "",
  dbSchemaFilter: "",
  dbSchemaExcludeFilter: "",
  dbTableFilter: "",
  dbTableExcludeFilter: "",
  dbSecretArn: "",
  dbWarehouse: "",
  dbRole: "",
  jdbcCrossAccountRoleArn: "",
  customConnectorName: "",
  customConnectorFunctionArn: "",
  customConnectorDatabaseName: "",
  customConnectorTableFilter: "",
  customConnectorTableExcludeFilter: "",
  metadataEnrichmentEnabled: true,
  docUploadName: "",
  docUploadFiles: [],
  docS3Name: "",
  docS3BucketArn: "",
  docS3Prefixes: [{ id: 0, value: "" }],
  docS3RoleArn: "",
  enableVersioning: true,
  deletePrevVersions: false,
  // Defaults match server-side behaviour today: infer classifications from the
  // corpus, and let the extractor name topics freely.
  vocabularyMode: "infer",
  preferredEntityClassifications: [],
  topicMode: "auto",
  preferredTopics: [],
};

// ── Validation ────────────────────────────────────────────────────────────────

/**
 * Validate cross-account IAM role ARN format.
 * Backend IAM policy restricts AssumeRole to roles matching:
 *   arn:aws:iam::*:role/{prefix}-{env}-datasource-access-*
 * Default prefix is "coa", default env is "dev", so pattern becomes:
 *   arn:aws:iam::ACCOUNT:role/coa-dev-datasource-access-SUFFIX
 */
function validateCrossAccountRoleArn(arn: string): string | undefined {
  if (!arn.trim()) return undefined; // optional field
  const roleArnPattern =
    /^arn:aws:iam::\d{12}:role\/[a-zA-Z0-9_+=,.@-]+-datasource-access-[a-zA-Z0-9_+=,.@-]+$/;
  if (!roleArnPattern.test(arn.trim())) {
    return "Role ARN must match pattern: arn:aws:iam::ACCOUNT:role/{prefix}-datasource-access-*";
  }
  return undefined;
}

/**
 * Validate a connector Lambda function ARN.
 * Mirrors the LambdaFunctionArn shape the backend accepts: a full ARN,
 * optionally qualified with a version, an alias, or $LATEST. Lambda's partial
 * ARN and name-only forms are rejected — the connector runs in the customer's
 * account, so an unqualified name would resolve against this deployment's
 * account instead.
 */
function validateLambdaFunctionArn(arn: string): string | undefined {
  if (!arn.trim()) return undefined; // callers decide whether the field is required
  const lambdaArnPattern =
    /^arn:aws[a-z-]*:lambda:[a-z0-9-]+:\d{12}:function:[a-zA-Z0-9_-]+(:(\$LATEST|[a-zA-Z0-9_-]+))?$/;
  if (!lambdaArnPattern.test(arn.trim())) {
    return "Must be a full Lambda function ARN: arn:aws:lambda:REGION:ACCOUNT:function:NAME";
  }
  return undefined;
}

interface ValidationErrors {
  step1?: string;
  dbName?: string;
  glueCatalogId?: string;
  glueDatabaseName?: string;
  glueCrossAccountRoleArn?: string;
  glueRedshiftWorkgroup?: string;
  jdbcName?: string;
  dbEngine?: string;
  dbHost?: string;
  dbPort?: string;
  dbDatabaseName?: string;
  dbSecretArn?: string;
  jdbcCrossAccountRoleArn?: string;
  customConnectorName?: string;
  customConnectorFunctionArn?: string;
  customConnectorDatabaseName?: string;
  docUploadName?: string;
  docFiles?: string;
  docS3Name?: string;
  docS3BucketArn?: string;
  preferredEntityClassifications?: string;
  preferredTopics?: string;
  docS3Prefixes?: Record<number, string>;
}

/** The name field that belongs to the selected source kind. A switch rather
 *  than a ternary chain so a new kind cannot silently inherit another's name. */
function sourceNameFor(model: Model, docTab: "upload" | "s3"): string {
  switch (model.sourceKind) {
    case "GLUE_DATABASE":
      return model.dbName;
    case "JDBC_DATABASE":
      return model.jdbcName;
    case "CUSTOM_CONNECTOR":
      return model.customConnectorName;
    case "DOCUMENTS":
      return docTab === "upload" ? model.docUploadName : model.docS3Name;
  }
}

function validateStep2(
  model: Model,
  docTab: "upload" | "s3",
): ValidationErrors {
  const errs: ValidationErrors = {};
  if (model.sourceKind === "GLUE_DATABASE") {
    if (!model.dbName.trim()) errs.dbName = "Name is required.";
    if (!model.glueCatalogId.trim())
      errs.glueCatalogId = "Catalog ID is required.";
    if (!model.glueDatabaseName.trim())
      errs.glueDatabaseName = "Database name is required.";
    const roleErr = validateCrossAccountRoleArn(model.glueCrossAccountRoleArn);
    if (roleErr) errs.glueCrossAccountRoleArn = roleErr;
    // A Redshift-executed Glue source must name its workgroup — the
    // backend cannot infer which Serverless workgroup to use.
    if (
      model.glueExecutionEngine === "REDSHIFT" &&
      !model.glueRedshiftWorkgroup.trim()
    )
      errs.glueRedshiftWorkgroup =
        "Redshift workgroup is required when the execution engine is Redshift.";
  } else if (model.sourceKind === "JDBC_DATABASE") {
    if (!model.jdbcName.trim()) errs.jdbcName = "Name is required.";
    if (!model.dbEngine) errs.dbEngine = "Select a database engine.";
    if (!model.dbHost.trim()) errs.dbHost = "Host is required.";
    if (!model.dbPort.trim()) errs.dbPort = "Port is required.";
    if (!model.dbDatabaseName.trim())
      errs.dbDatabaseName = "Database name is required.";
    if (!model.dbSecretArn.trim())
      errs.dbSecretArn = "Secrets Manager ARN is required.";
    const roleErr = validateCrossAccountRoleArn(model.jdbcCrossAccountRoleArn);
    if (roleErr) errs.jdbcCrossAccountRoleArn = roleErr;
  } else if (model.sourceKind === "CUSTOM_CONNECTOR") {
    if (!model.customConnectorName.trim())
      errs.customConnectorName = "Name is required.";
    if (!model.customConnectorFunctionArn.trim())
      errs.customConnectorFunctionArn = "Connector function ARN is required.";
    else {
      const arnErr = validateLambdaFunctionArn(
        model.customConnectorFunctionArn,
      );
      if (arnErr) errs.customConnectorFunctionArn = arnErr;
    }
    if (!model.customConnectorDatabaseName.trim())
      errs.customConnectorDatabaseName = "Database name is required.";
  } else {
    // DOCUMENTS
    if (docTab === "upload") {
      if (!model.docUploadName.trim()) errs.docUploadName = "Name is required.";
      if (model.docUploadFiles.length === 0)
        errs.docFiles = "Select at least one file.";
      else if (model.docUploadFiles.length > MAX_UPLOAD_FILES)
        errs.docFiles = `Maximum ${MAX_UPLOAD_FILES} files.`;
      else {
        const bad = model.docUploadFiles.filter(
          (f) => !SUPPORTED_UPLOAD_CONTENT_TYPES.has(resolveContentType(f)),
        );
        if (bad.length > 0)
          errs.docFiles = `Unsupported: ${bad.map((f) => f.name).join(", ")}`;
      }
    } else {
      if (!model.docS3Name.trim()) errs.docS3Name = "Name is required.";
      if (!model.docS3BucketArn.trim())
        errs.docS3BucketArn = "Bucket ARN is required.";
      else if (!S3_ARN_RE.test(model.docS3BucketArn.trim()))
        errs.docS3BucketArn = "Must be a valid S3 bucket ARN.";
      const prefixErrs: Record<number, string> = {};
      model.docS3Prefixes.forEach(({ id, value }) => {
        if (!value.trim()) return;
        const e = validateS3Prefix(value);
        if (e) prefixErrs[id] = e;
      });
      if (Object.keys(prefixErrs).length > 0) errs.docS3Prefixes = prefixErrs;
    }
    // "Define my own" with nothing defined is a contradiction: the payload would
    // silently fall back to inference (classes) or automatic naming (topics),
    // doing the opposite of what was selected. Block it rather than guess.
    // Applies to both document tabs, hence outside the upload/S3 branch.
    if (
      model.vocabularyMode === "custom" &&
      model.preferredEntityClassifications.length === 0
    ) {
      errs.preferredEntityClassifications =
        "Add at least one class, or choose “Infer from my documents”.";
    }
    if (model.topicMode === "custom" && model.preferredTopics.length === 0) {
      errs.preferredTopics = "Add at least one topic, or choose “Automatic”.";
    }
  }
  if (Object.keys(errs).length > 0) errs.step1 = "Fix the errors above.";
  return errs;
}

// ── Component ─────────────────────────────────────────────────────────────────

export const ConnectSource: React.FC = () => {
  const { namespaceId } = useParams<{ namespaceId: string }>();
  // The ExternalId shown for cross-account roles is derived server-side from the
  // namespace, so it comes from the namespace detail rather than being typed in.
  const { data: namespaceData, isLoading: namespaceLoading } = useGetNamespace(
    namespaceId ?? "",
  );
  const datasourceExternalId = namespaceData?.namespace?.datasourceExternalId;
  const navigate = useNavigate();

  const [model, setModel] = useState<Model>(initialModel);
  const [activeStepIndex, setActiveStepIndex] = useState(0);
  const [showErrors, setShowErrors] = useState<Record<number, boolean>>({});
  const [docTab, setDocTab] = useState<"upload" | "s3">("upload");
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const prefixIdCounter = useRef(1);

  const setField = <K extends keyof Model>(k: K, v: Model[K]) =>
    setModel((prev) => ({ ...prev, [k]: v }));

  const isDatabase =
    model.sourceKind === "GLUE_DATABASE" ||
    model.sourceKind === "JDBC_DATABASE" ||
    model.sourceKind === "CUSTOM_CONNECTOR";

  // ── Mutations ──────────────────────────────────────────────────────
  const {
    mutate: createSource,
    mutateAsync: createSourceAsync,
    isPending: isCreating,
    error: createError,
  } = useCreateSource({
    onSuccess: (result) => {
      const sourceId = result.body?.sourceId;
      navigate(
        sourceId
          ? `/namespaces/${namespaceId}/sources/${sourceId}`
          : `/namespaces/${namespaceId}/sources`,
      );
    },
  });

  const { mutateAsync: getUploadUrls, isPending: isGettingUrls } =
    useGetSourceUploadUrls(namespaceId ?? "");

  const isSubmitting = isCreating || isGettingUrls || uploadProgress !== null;

  // ── Prefix helpers ─────────────────────────────────────────────────
  // Use functional setModel updates so these callbacks have stable identities
  // and don't trigger a recompute of the steps useMemo on every render.
  const updatePrefix = useCallback((id: number, value: string) => {
    setModel((prev) => ({
      ...prev,
      docS3Prefixes: prev.docS3Prefixes.map((p) =>
        p.id === id ? { ...p, value } : p,
      ),
    }));
  }, []);
  const addPrefix = useCallback(() => {
    const id = prefixIdCounter.current++;
    setModel((prev) => ({
      ...prev,
      docS3Prefixes: [...prev.docS3Prefixes, { id, value: "" }],
    }));
  }, []);
  const removePrefix = useCallback((id: number) => {
    setModel((prev) => ({
      ...prev,
      docS3Prefixes: prev.docS3Prefixes.filter((p) => p.id !== id),
    }));
  }, []);

  // ── Submit ─────────────────────────────────────────────────────────
  const handleSubmit = async () => {
    const step2Errs = validateStep2(model, docTab);
    if (step2Errs.step1) {
      setShowErrors({ 0: true, 1: true });
      setActiveStepIndex(1);
      return;
    }

    if (model.sourceKind === "GLUE_DATABASE") {
      createSource({
        namespaceId: namespaceId!,
        body: {
          sourceType: SourceType.DATABASE,
          databaseSource: {
            name: model.dbName.trim(),
            metadataEnrichmentEnabled: model.metadataEnrichmentEnabled,
            glueConfiguration: {
              catalogId: model.glueCatalogId.trim(),
              region: model.glueRegion.trim(),
              databaseName: model.glueDatabaseName.trim(),
              tableFilter: model.glueTableFilter.trim() || undefined,
              tableExcludeFilter:
                model.glueTableExcludeFilter.trim() || undefined,
              athenaDataCatalogName:
                model.glueExecutionEngine === "REDSHIFT"
                  ? undefined
                  : model.glueAthenaDataCatalogName.trim() || undefined,
              crossAccountRoleArn:
                model.glueCrossAccountRoleArn.trim() || undefined,
              // Only send executionEngine/redshiftWorkgroup when the
              // user opts into Redshift — omitting them keeps ATHENA the default.
              executionEngine:
                model.glueExecutionEngine === "REDSHIFT"
                  ? "REDSHIFT"
                  : undefined,
              redshiftWorkgroup:
                model.glueExecutionEngine === "REDSHIFT"
                  ? model.glueRedshiftWorkgroup.trim()
                  : undefined,
            },
          },
        },
      });
      return;
    }

    if (model.sourceKind === "JDBC_DATABASE") {
      createSource({
        namespaceId: namespaceId!,
        body: {
          sourceType: SourceType.DATABASE,
          databaseSource: {
            name: model.jdbcName.trim(),
            metadataEnrichmentEnabled: model.metadataEnrichmentEnabled,
            jdbcConfiguration: {
              engine: model.dbEngine!,
              host: model.dbHost.trim(),
              port: parseInt(model.dbPort, 10),
              databaseName: model.dbDatabaseName.trim(),
              schemaFilter: model.dbSchemaFilter.trim() || undefined,
              schemaExcludeFilter:
                model.dbSchemaExcludeFilter.trim() || undefined,
              tableFilter: model.dbTableFilter.trim() || undefined,
              tableExcludeFilter:
                model.dbTableExcludeFilter.trim() || undefined,
              credentialSecretArn: model.dbSecretArn.trim(),
              warehouse: model.dbWarehouse.trim() || undefined,
              role: model.dbRole.trim() || undefined,
              crossAccountRoleArn:
                model.jdbcCrossAccountRoleArn.trim() || undefined,
            },
          },
        },
      });
      return;
    }

    if (model.sourceKind === "CUSTOM_CONNECTOR") {
      createSource({
        namespaceId: namespaceId!,
        body: {
          sourceType: SourceType.DATABASE,
          databaseSource: {
            name: model.customConnectorName.trim(),
            metadataEnrichmentEnabled: model.metadataEnrichmentEnabled,
            // customConnectorConfiguration only: the backend rejects a payload carrying
            // more than one of glue/jdbc/athena configuration, since the
            // configuration present is what selects the sub-type.
            customConnectorConfiguration: {
              connectorFunctionArn: model.customConnectorFunctionArn.trim(),
              databaseName: model.customConnectorDatabaseName.trim(),
              tableFilter: model.customConnectorTableFilter.trim() || undefined,
              tableExcludeFilter:
                model.customConnectorTableExcludeFilter.trim() || undefined,
            },
          },
        },
      });
      return;
    }

    // DOCUMENTS — s3 mode
    if (docTab === "s3") {
      createSource({
        namespaceId: namespaceId!,
        body: {
          sourceType: SourceType.DOCUMENTS,
          documentSource: {
            name: model.docS3Name.trim(),
            sourceBucketArn: model.docS3BucketArn.trim(),
            s3Prefixes: model.docS3Prefixes
              .map((p) => p.value.trim())
              .filter(Boolean),
            roleArn: model.docS3RoleArn.trim() || undefined,
            extractionConfig: buildExtractionConfig(model),
          },
        },
      });
      return;
    }

    // DOCUMENTS — upload mode
    setUploadError(null);
    setUploadProgress(0);
    try {
      const result = await getUploadUrls({
        files: model.docUploadFiles.map((f) => ({
          filename: f.name,
          contentType: resolveContentType(f),
        })),
      });
      const { uploadId, uploadUrls } = result;
      if (!uploadId || !uploadUrls)
        throw new Error("Invalid response: missing uploadId or uploadUrls.");

      const fileMap = new Map(model.docUploadFiles.map((f) => [f.name, f]));
      let completed = 0;
      for (const entry of uploadUrls) {
        if (!entry.filename)
          throw new Error(
            "Server returned an upload entry without a filename.",
          );
        const file = fileMap.get(entry.filename);
        if (!file || !entry.uploadUrl)
          throw new Error(`Missing upload URL for ${entry.filename}`);
        const resp = await fetch(entry.uploadUrl, {
          method: "PUT",
          body: file,
          headers: { "Content-Type": resolveContentType(file) },
        });
        if (!resp.ok)
          throw new Error(
            `Failed to upload ${entry.filename}: ${resp.status} ${resp.statusText}`,
          );
        completed++;
        setUploadProgress(Math.round((completed / uploadUrls.length) * 90));
      }
      setUploadProgress(95);
      await createSourceAsync({
        namespaceId: namespaceId!,
        body: {
          sourceType: SourceType.DOCUMENTS,
          documentSource: {
            name: model.docUploadName.trim(),
            uploadId,
            extractionConfig: buildExtractionConfig(model),
          },
        },
      });
    } catch (err) {
      setUploadProgress(null);
      // If the error is from createSourceAsync (a ControlPlaneServiceServiceException),
      // createError on the mutation already shows it — don't also set uploadError.
      if (!(err instanceof ControlPlaneServiceServiceException)) {
        setUploadError(
          extractErrorMessage(err, "Upload failed. Please try again."),
        );
      }
    }
  };

  // ── Steps ──────────────────────────────────────────────────────────
  const step2Errs = useMemo(
    () => (showErrors[1] ? validateStep2(model, docTab) : {}),
    [showErrors, model, docTab],
  );

  const steps: WizardProps.Step[] = useMemo(() => {
    // ── Step 1: Choose source type (3 tiles) ──────────────────────────
    const step1: WizardProps.Step = {
      title: "Choose source type",
      description: "Select the type of source you want to connect.",
      content: (
        <Container header={<Header variant="h2">Source type</Header>}>
          <Tiles
            value={model.sourceKind}
            onChange={({ detail }) => {
              if (isSourceKind(detail.value))
                setField("sourceKind", detail.value);
            }}
            items={[
              {
                value: "GLUE_DATABASE",
                label: SOURCE_KIND_LABELS.GLUE_DATABASE,
                description:
                  "Connect to an existing AWS Glue Data Catalog database. Covers S3/Iceberg tables, DynamoDB, and Athena-federated JDBC sources.",
              },
              {
                value: "JDBC_DATABASE",
                label: SOURCE_KIND_LABELS.JDBC_DATABASE,
                description: `Connect directly to a relational database. Schema is discovered from information_schema. Supported engines: ${ENGINE_LABELS}.`,
              },
              {
                value: "CUSTOM_CONNECTOR",
                label: SOURCE_KIND_LABELS.CUSTOM_CONNECTOR,
                description:
                  "Bring your own data source: register an Athena Query Federation SDK connector you built and deployed as a Lambda in your own AWS account. Schema and rows are read through Athena against that connector.",
              },
              {
                value: "DOCUMENTS",
                label: SOURCE_KIND_LABELS.DOCUMENTS,
                description:
                  "Ingest unstructured documents from an S3 bucket or by uploading files directly.",
              },
            ]}
          />
        </Container>
      ),
    };

    // ── Step 2: Configure connection ──────────────────────────────────
    let step2: WizardProps.Step;

    if (model.sourceKind === "GLUE_DATABASE") {
      step2 = {
        title: "Configure connection",
        description: "Select the Glue Data Catalog database to connect.",
        errorText: step2Errs.step1,
        content: (
          <SpaceBetween size="l">
            <Container header={<Header variant="h2">Source details</Header>}>
              <SpaceBetween size="l">
                <FormField
                  label={
                    <>
                      Source name{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description="A unique name for this source within the namespace."
                  errorText={step2Errs.dbName}
                >
                  <Input
                    value={model.dbName}
                    onChange={({ detail }) => setField("dbName", detail.value)}
                    placeholder="My Glue Database"
                  />
                </FormField>
              </SpaceBetween>
            </Container>
            <Container header={<Header variant="h2">Glue database</Header>}>
              <SpaceBetween size="l">
                <FormField
                  label={
                    <>
                      Catalog ID (AWS account){" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  errorText={step2Errs.glueCatalogId}
                >
                  <Input
                    value={model.glueCatalogId}
                    onChange={({ detail }) =>
                      setField("glueCatalogId", detail.value)
                    }
                    placeholder="123456789012"
                  />
                </FormField>
                <FormField
                  label={
                    <>
                      Region{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                >
                  <Input
                    value={model.glueRegion}
                    onChange={({ detail }) =>
                      setField("glueRegion", detail.value)
                    }
                    placeholder="us-east-1"
                  />
                </FormField>
                <FormField
                  label={
                    <>
                      Database name{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description="Choose a Glue Data Catalog database to connect. All tables within the database will be scanned."
                  errorText={step2Errs.glueDatabaseName}
                >
                  <Input
                    value={model.glueDatabaseName}
                    onChange={({ detail }) =>
                      setField("glueDatabaseName", detail.value)
                    }
                    placeholder="my_glue_db"
                  />
                </FormField>
              </SpaceBetween>
            </Container>
            <ExpandableSection
              headerText="Advanced configuration"
              variant="container"
            >
              <SpaceBetween size="l">
                <FormField
                  label="Table filter"
                  description="Optional regex to include specific tables."
                >
                  <Input
                    value={model.glueTableFilter}
                    onChange={({ detail }) =>
                      setField("glueTableFilter", detail.value)
                    }
                    placeholder="orders|customers"
                  />
                </FormField>
                <FormField
                  label="Table exclude filter"
                  description="Optional regex to exclude specific tables."
                >
                  <Input
                    value={model.glueTableExcludeFilter}
                    onChange={({ detail }) =>
                      setField("glueTableExcludeFilter", detail.value)
                    }
                    placeholder="tmp_.*|staging_.*"
                  />
                </FormField>
                <FormField
                  label="Execution engine"
                  description="How this Glue/Iceberg source's queries run at serve time. Redshift runs them via Redshift Serverless (awsdatacatalog auto-mount) instead of Athena. This does NOT connect to a Redshift database — to query a Redshift cluster's own tables, onboard a JDBC source instead."
                >
                  <Select
                    selectedOption={
                      GLUE_EXECUTION_ENGINE_OPTIONS.find(
                        (o) => o.value === model.glueExecutionEngine,
                      ) ?? null
                    }
                    onChange={({ detail }) => {
                      const selected = GLUE_EXECUTION_ENGINE_OPTIONS.find(
                        (o) => o.value === detail.selectedOption.value,
                      );
                      if (selected)
                        setField("glueExecutionEngine", selected.value);
                    }}
                    options={GLUE_EXECUTION_ENGINE_OPTIONS}
                  />
                </FormField>
                {model.glueExecutionEngine === "REDSHIFT" && (
                  <FormField
                    label={
                      <>
                        Redshift workgroup{" "}
                        <Box variant="span" color="text-status-error">
                          *
                        </Box>
                      </>
                    }
                    description="The Amazon Redshift Serverless workgroup that executes this source's queries."
                    errorText={step2Errs.glueRedshiftWorkgroup}
                  >
                    <Input
                      value={model.glueRedshiftWorkgroup}
                      onChange={({ detail }) =>
                        setField("glueRedshiftWorkgroup", detail.value)
                      }
                      placeholder="my-workgroup"
                    />
                  </FormField>
                )}
                {model.glueExecutionEngine !== "REDSHIFT" && (
                  <FormField
                    label="Athena DataCatalog name"
                    description="Optional. Required for JDBC-backed Glue databases to enable federated queries."
                  >
                    <Input
                      value={model.glueAthenaDataCatalogName}
                      onChange={({ detail }) =>
                        setField("glueAthenaDataCatalogName", detail.value)
                      }
                      placeholder="my_federated_catalog"
                    />
                  </FormField>
                )}
                <FormField
                  label="Cross-account role ARN"
                  description="Optional. For a Glue catalog in another account, the IAM role Context Ontology Accelerator assumes to read catalog metadata. Must be named {prefix}-datasource-access-*, and its trust policy must require the External ID below."
                  errorText={step2Errs.glueCrossAccountRoleArn}
                >
                  <Input
                    value={model.glueCrossAccountRoleArn}
                    onChange={({ detail }) =>
                      setField("glueCrossAccountRoleArn", detail.value)
                    }
                    placeholder="arn:aws:iam::222222222222:role/coa-dev-datasource-access-acme"
                  />
                </FormField>
                <CrossAccountExternalIdField
                  externalId={datasourceExternalId}
                  isLoading={namespaceLoading}
                />
              </SpaceBetween>
            </ExpandableSection>
          </SpaceBetween>
        ),
      };
    } else if (model.sourceKind === "JDBC_DATABASE") {
      step2 = {
        title: "Configure connection",
        description: "Provide connection details for your JDBC database.",
        errorText: step2Errs.step1,
        content: (
          <SpaceBetween size="l">
            <Container header={<Header variant="h2">Source details</Header>}>
              <FormField
                label={
                  <>
                    Source name{" "}
                    <Box variant="span" color="text-status-error">
                      *
                    </Box>
                  </>
                }
                description="A unique name for this source within the namespace."
                errorText={step2Errs.jdbcName}
              >
                <Input
                  value={model.jdbcName}
                  onChange={({ detail }) => setField("jdbcName", detail.value)}
                  placeholder="My JDBC Database"
                />
              </FormField>
            </Container>
            <Container
              header={<Header variant="h2">Connection settings</Header>}
            >
              <SpaceBetween size="l">
                <FormField
                  label={
                    <>
                      Database engine{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  errorText={step2Errs.dbEngine}
                >
                  <Select
                    selectedOption={
                      model.dbEngine
                        ? (ENGINE_OPTIONS.find(
                            (o) => o.value === model.dbEngine,
                          ) ?? null)
                        : null
                    }
                    onChange={({ detail }) => {
                      const eng = detail.selectedOption.value as DatabaseEngine;
                      setField("dbEngine", eng);
                      setField("dbPort", DEFAULT_PORTS[eng]);
                    }}
                    options={ENGINE_OPTIONS}
                    placeholder="Select an engine"
                  />
                </FormField>
                <FormField
                  label={
                    <>
                      Host{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  errorText={step2Errs.dbHost}
                >
                  <Input
                    value={model.dbHost}
                    onChange={({ detail }) => setField("dbHost", detail.value)}
                    placeholder="db.example.com"
                  />
                </FormField>
                <FormField
                  label={
                    <>
                      Port{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  errorText={step2Errs.dbPort}
                >
                  <Input
                    value={model.dbPort}
                    onChange={({ detail }) => setField("dbPort", detail.value)}
                    inputMode="numeric"
                  />
                </FormField>
                <FormField
                  label={
                    <>
                      Database name{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  errorText={step2Errs.dbDatabaseName}
                >
                  <Input
                    value={model.dbDatabaseName}
                    onChange={({ detail }) =>
                      setField("dbDatabaseName", detail.value)
                    }
                    placeholder="my_database"
                  />
                </FormField>
                <FormField
                  label={
                    <>
                      Credential secret ARN{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description="AWS Secrets Manager secret containing the database username and password."
                  errorText={step2Errs.dbSecretArn}
                >
                  <Input
                    value={model.dbSecretArn}
                    onChange={({ detail }) =>
                      setField("dbSecretArn", detail.value)
                    }
                    placeholder="arn:aws:secretsmanager:..."
                  />
                </FormField>
              </SpaceBetween>
            </Container>
            <ExpandableSection
              headerText="Advanced configuration"
              variant="container"
            >
              <SpaceBetween size="l">
                <FormField
                  label="Schema filter"
                  description="Optional glob(s) (* ? [seq]) to include specific schemas. Separate multiple with | or ,."
                >
                  <Input
                    value={model.dbSchemaFilter}
                    onChange={({ detail }) =>
                      setField("dbSchemaFilter", detail.value)
                    }
                    placeholder="public|sales"
                  />
                </FormField>
                <FormField
                  label="Schema exclude filter"
                  description="Optional glob(s) (* ? [seq]) to exclude specific schemas. Separate multiple with | or ,."
                >
                  <Input
                    value={model.dbSchemaExcludeFilter}
                    onChange={({ detail }) =>
                      setField("dbSchemaExcludeFilter", detail.value)
                    }
                    placeholder="temp_*|staging_*"
                  />
                </FormField>
                <FormField
                  label="Table filter"
                  description="Optional glob(s) (* ? [seq]) to include specific tables. Separate multiple with | or ,."
                >
                  <Input
                    value={model.dbTableFilter}
                    onChange={({ detail }) =>
                      setField("dbTableFilter", detail.value)
                    }
                    placeholder="orders|customers"
                  />
                </FormField>
                <FormField
                  label="Table exclude filter"
                  description="Optional glob(s) (* ? [seq]) to exclude specific tables. Separate multiple with | or ,."
                >
                  <Input
                    value={model.dbTableExcludeFilter}
                    onChange={({ detail }) =>
                      setField("dbTableExcludeFilter", detail.value)
                    }
                    placeholder="tmp_*|staging_*"
                  />
                </FormField>
                {/* Snowflake-specific fields — shown when the user selects
                    SNOWFLAKE as the engine. */}
                {model.dbEngine === "SNOWFLAKE" && (
                  <>
                    <FormField
                      label="Warehouse"
                      description="Snowflake virtual warehouse for discovery queries."
                    >
                      <Input
                        value={model.dbWarehouse}
                        onChange={({ detail }) =>
                          setField("dbWarehouse", detail.value)
                        }
                        placeholder="COMPUTE_WH"
                      />
                    </FormField>
                    <FormField
                      label="Role"
                      description="Optional Snowflake RBAC role to assume."
                    >
                      <Input
                        value={model.dbRole}
                        onChange={({ detail }) =>
                          setField("dbRole", detail.value)
                        }
                        placeholder="ACCOUNTADMIN"
                      />
                    </FormField>
                  </>
                )}
                <FormField
                  label="Cross-account role ARN"
                  description="Optional. For a credential secret in another account, the IAM role Context Ontology Accelerator assumes to read it. Must be named {prefix}-datasource-access-*, and its trust policy must require the External ID below."
                  errorText={step2Errs.jdbcCrossAccountRoleArn}
                >
                  <Input
                    value={model.jdbcCrossAccountRoleArn}
                    onChange={({ detail }) =>
                      setField("jdbcCrossAccountRoleArn", detail.value)
                    }
                    placeholder="arn:aws:iam::222222222222:role/coa-dev-datasource-access-acme"
                  />
                </FormField>
                <CrossAccountExternalIdField
                  externalId={datasourceExternalId}
                  isLoading={namespaceLoading}
                />
              </SpaceBetween>
            </ExpandableSection>
          </SpaceBetween>
        ),
      };
    } else if (model.sourceKind === "CUSTOM_CONNECTOR") {
      step2 = {
        title: "Configure connection",
        description:
          "Point at the Athena Query Federation connector you deployed.",
        errorText: step2Errs.step1,
        content: (
          <SpaceBetween size="l">
            <Container header={<Header variant="h2">Source details</Header>}>
              <FormField
                label={
                  <>
                    Source name{" "}
                    <Box variant="span" color="text-status-error">
                      *
                    </Box>
                  </>
                }
                description="A unique name for this source within the namespace."
                errorText={step2Errs.customConnectorName}
              >
                <Input
                  value={model.customConnectorName}
                  onChange={({ detail }) =>
                    setField("customConnectorName", detail.value)
                  }
                  placeholder="My Custom Connector"
                />
              </FormField>
            </Container>
            <Container header={<Header variant="h2">Connector</Header>}>
              <SpaceBetween size="l">
                <FormField
                  label={
                    <>
                      Connector function ARN{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description="The connector Lambda. It serves both metadata and record requests. It must live in the same AWS Region as Context Ontology Accelerator; a connector in another Region is rejected, because Athena can only invoke a data source connector co-located with the query."
                  errorText={step2Errs.customConnectorFunctionArn}
                >
                  <Input
                    value={model.customConnectorFunctionArn}
                    onChange={({ detail }) =>
                      setField("customConnectorFunctionArn", detail.value)
                    }
                    placeholder="arn:aws:lambda:us-east-1:123456789012:function:my-connector"
                  />
                </FormField>
                <FormField
                  label={
                    <>
                      Database name{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description="The single database inside the connector's catalog that this source exposes. A connector serving several databases is onboarded once per database."
                  errorText={step2Errs.customConnectorDatabaseName}
                >
                  <Input
                    value={model.customConnectorDatabaseName}
                    onChange={({ detail }) =>
                      setField("customConnectorDatabaseName", detail.value)
                    }
                    placeholder="my_connector_db"
                  />
                </FormField>
              </SpaceBetween>
            </Container>
            <ExpandableSection
              headerText="Advanced configuration"
              variant="container"
            >
              <SpaceBetween size="l">
                <FormField
                  label="Table filter"
                  description="Optional glob(s) (* ? [seq]) to include specific tables. Separate multiple with | or ,."
                >
                  <Input
                    value={model.customConnectorTableFilter}
                    onChange={({ detail }) =>
                      setField("customConnectorTableFilter", detail.value)
                    }
                    placeholder="orders|customers"
                  />
                </FormField>
                <FormField
                  label="Table exclude filter"
                  description="Optional glob(s) (* ? [seq]) to exclude specific tables. Applied after the include filter. Separate multiple with | or ,."
                >
                  <Input
                    value={model.customConnectorTableExcludeFilter}
                    onChange={({ detail }) =>
                      setField(
                        "customConnectorTableExcludeFilter",
                        detail.value,
                      )
                    }
                    placeholder="tmp_*|staging_*"
                  />
                </FormField>
              </SpaceBetween>
            </ExpandableSection>
          </SpaceBetween>
        ),
      };
    } else {
      // DOCUMENTS
      step2 = {
        title: "Configure source",
        description: "Upload files or connect an S3 bucket.",
        errorText: step2Errs.step1,
        content: (
          <SpaceBetween size="l">
            <Tabs
              activeTabId={docTab}
              onChange={({ detail }) =>
                setDocTab(detail.activeTabId as "upload" | "s3")
              }
              tabs={[
                {
                  id: "upload",
                  label: "Upload files",
                  content: (
                    <SpaceBetween size="l">
                      <Container
                        header={<Header variant="h2">Source details</Header>}
                      >
                        <SpaceBetween size="l">
                          <FormField
                            label={
                              <>
                                Name{" "}
                                <Box variant="span" color="text-status-error">
                                  *
                                </Box>
                              </>
                            }
                            errorText={step2Errs.docUploadName}
                          >
                            <Input
                              value={model.docUploadName}
                              onChange={({ detail }) =>
                                setField("docUploadName", detail.value)
                              }
                              placeholder="e.g. product-docs"
                            />
                          </FormField>
                          <FormField
                            label={
                              <>
                                Files{" "}
                                <Box variant="span" color="text-status-error">
                                  *
                                </Box>
                              </>
                            }
                            description="PDF, DOCX, TXT, or Markdown. Maximum 100 files."
                            errorText={step2Errs.docFiles}
                          >
                            <FileUpload
                              value={model.docUploadFiles}
                              onChange={({ detail }) =>
                                setField("docUploadFiles", detail.value)
                              }
                              accept=".pdf,.docx,.txt,.md,.markdown"
                              multiple
                              showFileSize
                              showFileThumbnail={false}
                              i18nStrings={{
                                uploadButtonText: (m) =>
                                  m ? "Choose files" : "Choose file",
                                dropzoneText: (m) =>
                                  m
                                    ? "Drop files to upload"
                                    : "Drop file to upload",
                                removeFileAriaLabel: (i) =>
                                  `Remove file ${i + 1}`,
                                limitShowFewer: "Show fewer files",
                                limitShowMore: "Show more files",
                                errorIconAriaLabel: "Error",
                              }}
                            />
                          </FormField>
                          {uploadProgress !== null && (
                            <ProgressBar
                              value={uploadProgress}
                              label="Uploading files"
                              description={
                                uploadProgress < 95
                                  ? `Uploading ${model.docUploadFiles.length} file(s)...`
                                  : "Triggering ingestion..."
                              }
                            />
                          )}
                        </SpaceBetween>
                      </Container>
                    </SpaceBetween>
                  ),
                },
                {
                  id: "s3",
                  label: "S3 bucket",
                  content: (
                    <SpaceBetween size="l">
                      <Container
                        header={<Header variant="h2">Source details</Header>}
                      >
                        <SpaceBetween size="l">
                          <FormField
                            label={
                              <>
                                Name{" "}
                                <Box variant="span" color="text-status-error">
                                  *
                                </Box>
                              </>
                            }
                            errorText={step2Errs.docS3Name}
                          >
                            <Input
                              value={model.docS3Name}
                              onChange={({ detail }) =>
                                setField("docS3Name", detail.value)
                              }
                              placeholder="e.g. product-docs"
                            />
                          </FormField>
                          <FormField
                            label={
                              <>
                                Source bucket ARN{" "}
                                <Box variant="span" color="text-status-error">
                                  *
                                </Box>
                              </>
                            }
                            errorText={step2Errs.docS3BucketArn}
                          >
                            <Input
                              value={model.docS3BucketArn}
                              onChange={({ detail }) =>
                                setField("docS3BucketArn", detail.value)
                              }
                              placeholder="arn:aws:s3:::my-bucket"
                            />
                          </FormField>
                          <FormField
                            label="S3 prefixes"
                            description="Leave blank to ingest the entire bucket."
                          >
                            <SpaceBetween size="xs">
                              {model.docS3Prefixes.map(({ id, value }, idx) => (
                                <SpaceBetween
                                  key={id}
                                  direction="horizontal"
                                  size="xs"
                                >
                                  <FormField
                                    errorText={step2Errs.docS3Prefixes?.[id]}
                                  >
                                    <Input
                                      value={value}
                                      onChange={({ detail }) =>
                                        updatePrefix(id, detail.value)
                                      }
                                      placeholder="documents/"
                                      ariaLabel={`S3 prefix ${idx + 1}`}
                                    />
                                  </FormField>
                                  {model.docS3Prefixes.length > 1 && (
                                    <Box padding={{ top: "xs" }}>
                                      <Button
                                        iconName="close"
                                        variant="icon"
                                        ariaLabel="Remove prefix"
                                        onClick={() => removePrefix(id)}
                                      />
                                    </Box>
                                  )}
                                </SpaceBetween>
                              ))}
                              <Button iconName="add-plus" onClick={addPrefix}>
                                Add prefix
                              </Button>
                            </SpaceBetween>
                          </FormField>
                        </SpaceBetween>
                      </Container>
                      <ExpandableSection
                        headerText="Cross-account access (optional)"
                        variant="container"
                      >
                        <FormField label="Role ARN">
                          <Input
                            value={model.docS3RoleArn}
                            onChange={({ detail }) =>
                              setField("docS3RoleArn", detail.value)
                            }
                            placeholder="arn:aws:iam::123456789012:role/my-role"
                          />
                        </FormField>
                      </ExpandableSection>
                    </SpaceBetween>
                  ),
                },
              ]}
            />
            {/* Its own section: this configures what the extractor looks for,
                which is a different concern from the document-lifecycle toggles
                in "Advanced options" below. Keeping them apart avoids implying a
                relationship that does not exist. The header names both fields
                rather than just the first, and "vocabulary" is no longer reused
                as a field label — it was previously both the section name and
                the name of one of the two things inside it. */}
            <ExpandableSection
              headerText="Entity classes and topics (optional)"
              variant="container"
            >
              <SpaceBetween size="l">
                <FormField
                  label="Entity classes"
                  description="What the extractor calls each thing it finds — a brand, a policy, a material. Each entity is recorded with its class, and those classes become classes in the ontology."
                  constraintText={
                    model.vocabularyMode === "custom"
                      ? `Enter one at a time, or paste several separated by commas. ${MAX_CLASSIFICATION_LENGTH} characters each; 15–25 classes works best.`
                      : undefined
                  }
                  characterCountText={
                    model.vocabularyMode === "custom"
                      ? `${model.preferredEntityClassifications.length} of ${MAX_VOCABULARY_ENTRIES} classes`
                      : undefined
                  }
                  warningText={
                    model.vocabularyMode === "custom"
                      ? vocabularyWarning(model.preferredEntityClassifications)
                      : undefined
                  }
                  errorText={step2Errs.preferredEntityClassifications}
                >
                  <SpaceBetween size="s">
                    <RadioGroup
                      value={model.vocabularyMode}
                      onChange={({ detail }) => {
                        if (isVocabularyMode(detail.value)) {
                          setField("vocabularyMode", detail.value);
                        }
                      }}
                      items={[
                        {
                          value: "infer",
                          label: "Infer from my documents",
                          description:
                            "Recommended. Classes are derived from a sample of your documents at ingest.",
                        },
                        {
                          value: "custom",
                          label: "Define my own",
                          description:
                            "Your classes are preferred, and a new one is created when nothing on your list fits. No extra classes are derived from your documents.",
                        },
                      ]}
                    />
                    {model.vocabularyMode === "custom" && (
                      <TokenInput
                        value={model.preferredEntityClassifications}
                        onChange={(next) =>
                          setField("preferredEntityClassifications", next)
                        }
                        placeholder="e.g. Loss Ratio, Claim Handler"
                        ariaLabel="Entity class labels"
                        itemLabelPlural="classes"
                        validate={(candidate, existing) =>
                          validateClassification(candidate, existing)
                        }
                      />
                    )}
                  </SpaceBetween>
                </FormField>
                <FormField
                  label="Topics"
                  description="What a passage is about, rather than what a thing is. Topics group related passages together and drive topic-based retrieval."
                  constraintText={
                    model.topicMode === "custom"
                      ? `Enter one at a time, or paste several separated by commas. Phrases are fine, and your capitalisation is kept. ${MAX_TOPIC_LENGTH} characters each.`
                      : undefined
                  }
                  characterCountText={
                    model.topicMode === "custom"
                      ? `${model.preferredTopics.length} of ${MAX_VOCABULARY_ENTRIES} topics`
                      : undefined
                  }
                  warningText={
                    model.topicMode === "custom"
                      ? topicWarning(model.preferredTopics)
                      : undefined
                  }
                  errorText={step2Errs.preferredTopics}
                >
                  <SpaceBetween size="s">
                    <RadioGroup
                      value={model.topicMode}
                      onChange={({ detail }) => {
                        if (isTopicMode(detail.value)) {
                          setField("topicMode", detail.value);
                        }
                      }}
                      items={[
                        {
                          value: "auto",
                          label: "Automatic",
                          description:
                            "Recommended. Topics are extracted from your documents.",
                        },
                        {
                          value: "custom",
                          label: "Define my own",
                          description:
                            "Topics are still extracted from your documents; your names are used where one matches. Expect topics beyond your list.",
                        },
                      ]}
                    />
                    {model.topicMode === "custom" && (
                      <TokenInput
                        value={model.preferredTopics}
                        onChange={(next) => setField("preferredTopics", next)}
                        placeholder="e.g. Black Tie Gala, Quiet Luxury"
                        ariaLabel="Preferred topic names"
                        itemLabelPlural="topics"
                        validate={(candidate, existing) =>
                          validateTopic(candidate, existing)
                        }
                      />
                    )}
                  </SpaceBetween>
                </FormField>
              </SpaceBetween>
            </ExpandableSection>
            <ExpandableSection
              headerText="Advanced options"
              variant="container"
            >
              <SpaceBetween size="l">
                <FormField description="Tracks document versions in Neptune.">
                  <Toggle
                    checked={model.enableVersioning}
                    onChange={({ detail }) =>
                      setField("enableVersioning", detail.checked)
                    }
                  >
                    Enable versioning
                  </Toggle>
                </FormField>
                <FormField description="Deletes previous versions after re-ingestion.">
                  <Toggle
                    checked={model.deletePrevVersions}
                    disabled={!model.enableVersioning}
                    onChange={({ detail }) =>
                      setField("deletePrevVersions", detail.checked)
                    }
                  >
                    Delete previous versions on re-ingestion
                  </Toggle>
                </FormField>
              </SpaceBetween>
            </ExpandableSection>
          </SpaceBetween>
        ),
      };
    }

    // ── Step 3: Enrichment (database only) ────────────────────────────
    const enrichmentStep: WizardProps.Step = {
      title: "Configure enrichment",
      description: "Choose AI enrichment features.",
      content: (
        <Container header={<Header variant="h2">Metadata enrichment</Header>}>
          <SpaceBetween size="l">
            <FormField description="When disabled, only technical metadata is captured during the scan. AI-generated descriptions, synonyms, glossary terms, and tags are skipped.">
              <Toggle
                checked={model.metadataEnrichmentEnabled}
                onChange={({ detail }) =>
                  setField("metadataEnrichmentEnabled", detail.checked)
                }
              >
                Enable AI metadata enrichment
              </Toggle>
            </FormField>
            <SpaceBetween size="s">
              <Box variant="h4">Auto-generate descriptions</Box>
              <Checkbox checked={model.metadataEnrichmentEnabled} disabled>
                Generate business-friendly descriptions for tables and columns
              </Checkbox>
              <Box variant="small" color="text-body-secondary">
                1–2 sentence business-friendly description per table and column.
                Tagged AI_GENERATED.
              </Box>
            </SpaceBetween>
            <SpaceBetween size="s">
              <Box variant="h4">Generate synonyms</Box>
              <Checkbox checked={model.metadataEnrichmentEnabled} disabled>
                Generate business-language alternatives for tables and columns
              </Checkbox>
              <Box variant="small" color="text-body-secondary">
                2–5 business-language alternatives per table and column.
              </Box>
            </SpaceBetween>
            <SpaceBetween size="s">
              <Box variant="h4">Generate glossary terms</Box>
              <Checkbox checked={model.metadataEnrichmentEnabled} disabled>
                Map tables and columns to business domain concepts
              </Checkbox>
              <Box variant="small" color="text-body-secondary">
                1–3 business domain concepts (e.g., "Order Management",
                "Revenue").
              </Box>
            </SpaceBetween>
            <SpaceBetween size="s">
              <Box variant="h4">Constraint discovery</Box>
              <Checkbox checked={model.metadataEnrichmentEnabled} disabled>
                Enable AI-assisted cross-table relationship inference
              </Checkbox>
              <Box variant="small" color="text-body-secondary">
                AI-inferred relationships are tagged as AI_INFERRED and require
                steward review before promotion.
              </Box>
            </SpaceBetween>
          </SpaceBetween>
        </Container>
      ),
    };

    // ── Step 3/4: Review ──────────────────────────────────────────────
    const reviewStep: WizardProps.Step = {
      title: "Review and connect",
      description: "Review your configuration before connecting.",
      content: (
        <SpaceBetween size="l">
          <Container header={<Header variant="h2">Source type</Header>}>
            <KeyValuePairs
              columns={2}
              items={[
                {
                  label: "Category",
                  value: SOURCE_KIND_LABELS[model.sourceKind],
                },
                {
                  label: "Source name",
                  value: sourceNameFor(model, docTab),
                },
              ]}
            />
          </Container>
          {model.sourceKind === "GLUE_DATABASE" && (
            <Container header={<Header variant="h2">Connection</Header>}>
              <KeyValuePairs
                columns={2}
                items={[
                  { label: "Catalog ID", value: model.glueCatalogId },
                  { label: "Region", value: model.glueRegion },
                  { label: "Database", value: model.glueDatabaseName },
                  {
                    label: "Table filter",
                    value: model.glueTableFilter || "(none)",
                  },
                ]}
              />
            </Container>
          )}
          {model.sourceKind === "JDBC_DATABASE" && (
            <Container header={<Header variant="h2">Connection</Header>}>
              <KeyValuePairs
                columns={2}
                items={[
                  {
                    label: "Engine",
                    value:
                      ENGINE_OPTIONS.find((o) => o.value === model.dbEngine)
                        ?.label ?? "—",
                  },
                  { label: "Host", value: model.dbHost },
                  { label: "Port", value: model.dbPort },
                  { label: "Database name", value: model.dbDatabaseName },
                  {
                    label: "Schema filter",
                    value: model.dbSchemaFilter || "(none)",
                  },
                  {
                    label: "Table filter",
                    value: model.dbTableFilter || "(none)",
                  },
                  {
                    label: "Authentication",
                    value: "AWS Secrets Manager",
                  },
                ]}
              />
            </Container>
          )}
          {model.sourceKind === "CUSTOM_CONNECTOR" && (
            <Container header={<Header variant="h2">Connection</Header>}>
              <KeyValuePairs
                columns={2}
                items={[
                  {
                    label: "Connector function ARN",
                    value: model.customConnectorFunctionArn,
                  },
                  {
                    label: "Database",
                    value: model.customConnectorDatabaseName,
                  },
                  {
                    label: "Table filter",
                    value: model.customConnectorTableFilter || "(none)",
                  },
                ]}
              />
            </Container>
          )}
          {model.sourceKind === "DOCUMENTS" && (
            <Container header={<Header variant="h2">Source</Header>}>
              <KeyValuePairs
                columns={2}
                items={[
                  {
                    label: "Mode",
                    value: docTab === "upload" ? "File upload" : "S3 bucket",
                  },
                  ...(docTab === "s3"
                    ? [{ label: "Bucket ARN", value: model.docS3BucketArn }]
                    : [
                        {
                          label: "Files",
                          value: `${model.docUploadFiles.length} file(s)`,
                        },
                      ]),
                ]}
              />
            </Container>
          )}
          {isDatabase && (
            <Container
              header={<Header variant="h2">Enrichment settings</Header>}
            >
              <KeyValuePairs
                columns={2}
                items={[
                  {
                    label: "Metadata enrichment",
                    value: model.metadataEnrichmentEnabled
                      ? "Enabled"
                      : "Disabled",
                  },
                ]}
              />
            </Container>
          )}
          {isDatabase && (
            <Alert type="info" header="What happens next">
              {model.metadataEnrichmentEnabled
                ? `Context Ontology Accelerator will start the scan and enrichment pipeline. The data source status changes to Scanning while technical metadata is discovered, then to Enriching while AI generates business descriptions, synonyms, glossary terms, and tags. Once complete, the status changes to Pending review — you will be notified to review and approve the enriched metadata before it is used for ontology induction.`
                : `Context Ontology Accelerator will scan the source and discover technical metadata only. AI metadata enrichment is disabled, so no business descriptions, synonyms, glossary terms, or tags will be generated. The status changes to Scanning, then directly to Pending review for steward approval.`}
            </Alert>
          )}
          {uploadError && (
            <Alert type="error" header="Upload failed">
              {uploadError}
            </Alert>
          )}
          {createError && (
            <Alert type="error" header="Failed to connect source">
              {extractErrorMessage(createError)}
            </Alert>
          )}
        </SpaceBetween>
      ),
    };

    return isDatabase
      ? [step1, step2, enrichmentStep, reviewStep]
      : [step1, step2, reviewStep];
  }, [
    model,
    docTab,
    step2Errs,
    uploadError,
    createError,
    uploadProgress,
    isDatabase,
    addPrefix,
    removePrefix,
    updatePrefix,
  ]);

  return (
    <Wizard
      steps={steps}
      activeStepIndex={activeStepIndex}
      onNavigate={({ detail }) => {
        const requested = detail.requestedStepIndex;
        if (requested > activeStepIndex) {
          if (activeStepIndex === 1) {
            const errs = validateStep2(model, docTab);
            if (errs.step1) {
              setShowErrors((p) => ({ ...p, 1: true }));
              return;
            }
          }
        }
        setActiveStepIndex(requested);
      }}
      onCancel={() => navigate(`/namespaces/${namespaceId}/sources`)}
      onSubmit={() => {
        void handleSubmit();
      }}
      isLoadingNextStep={isSubmitting}
      i18nStrings={{
        stepNumberLabel: (n) => `Step ${n}`,
        collapsedStepsLabel: (step, total) => `Step ${step} of ${total}`,
        cancelButton: "Cancel",
        previousButton: "Previous",
        nextButton: "Next",
        submitButton: "Connect source",
      }}
    />
  );
};
