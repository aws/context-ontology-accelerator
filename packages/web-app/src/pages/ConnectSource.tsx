// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useCallback, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useBreadcrumbTrail } from "@api-hooks/use-breadcrumb-trail";
import { useNamespace } from "@api-hooks/use-namespace";
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
import "./pages.css";

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
  loadFailed,
}: {
  readonly externalId: string | undefined;
  readonly isLoading: boolean;
  /** The namespace request failed, as opposed to a namespace that loaded but
   *  carries no ExternalId. Only the first is worth reloading the page for. */
  readonly loadFailed: boolean;
}): React.JSX.Element {
  const { t } = useAppI18n();
  // errorText rather than a Box: it renders in an assertive live region, so the
  // unavailable state reaches a screen reader even though nothing here is focusable.
  let errorText: string | undefined;
  if (!externalId && !isLoading) {
    errorText = loadFailed
      ? t(
          "Unavailable — the namespace could not be loaded. Reload the page to retrieve it.",
        )
      : t(
          "Unavailable — this namespace carries no External ID, so the role's trust policy cannot be bound to it. Report this to the operator of this deployment.",
        );
  }
  return (
    <FormField
      label={t("External ID (add to the role's trust policy)")}
      description={t(
        "Context Ontology Accelerator presents this value when assuming the role above. Add it as an sts:ExternalId condition on that role's trust policy — without the condition the role is assumable on behalf of any namespace in this deployment.",
      )}
      errorText={errorText}
    >
      {externalId ? (
        <CopyToClipboard
          copyButtonAriaLabel={t("Copy External ID")}
          copyButtonText={t("Copy")}
          copySuccessText={t("External ID copied")}
          copyErrorText={t("Failed to copy External ID")}
          textToCopy={externalId}
          variant="inline"
        />
      ) : isLoading ? (
        <Box color="text-status-inactive">{t("Loading…")}</Box>
      ) : null}
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
import { useAppI18n, type AppTranslate } from "../i18n/LocaleProvider";
import type { AppMessage } from "../i18n/appMessages";

// ── Source type options ───────────────────────────────────────────────────────

// Five-way selection shown in step 1
type SourceKind =
  | "GLUE_DATABASE"
  | "JDBC_DATABASE"
  | "CUSTOM_CONNECTOR"
  | "DATABRICKS_SQL_WAREHOUSE"
  | "DOCUMENTS";

/** Narrow a Tiles selection to a SourceKind. Cloudscape hands back a plain
 *  string, and a guard keeps that boundary honest without a cast. */
function isSourceKind(value: string): value is SourceKind {
  return (
    value === "GLUE_DATABASE" ||
    value === "JDBC_DATABASE" ||
    value === "CUSTOM_CONNECTOR" ||
    value === "DATABRICKS_SQL_WAREHOUSE" ||
    value === "DOCUMENTS"
  );
}

// Labels for the step-1 tiles, reused by the review step's summary.
const SOURCE_KIND_LABELS: Record<SourceKind, AppMessage> = {
  GLUE_DATABASE: "Glue database",
  JDBC_DATABASE: "JDBC database",
  CUSTOM_CONNECTOR: "Custom connector",
  // The sub-type covers a SQL Warehouse only — not Databricks jobs, Delta
  // Sharing or a cluster endpoint.
  DATABRICKS_SQL_WAREHOUSE: "Databricks SQL Warehouse",
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
  label: AppMessage;
  description: AppMessage;
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
  // Databricks: this platform deploys the connector Lambda, so there is no function
  // ARN — only the warehouse, the credential secret, and the role that reads it.
  databricksName: string;
  databricksWorkspaceHostname: string;
  databricksHttpPath: string;
  databricksCatalog: string;
  // `databaseName` on the wire, one Unity Catalog schema per source.
  databricksSchemaName: string;
  databricksSecretArn: string;
  databricksCrossAccountRoleArn: string;
  databricksTableFilter: string;
  databricksTableExcludeFilter: string;
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
  // Splitter chunk size: "default" sends nothing (the toolkit's 256 tokens),
  // "large" sends LARGE_CHUNK_SIZE, "custom" sends the two numbers below.
  chunkSizeMode: ChunkSizeMode;
  chunkSize: string;
  chunkOverlap: string;
}

/** How the document splitter's chunk size is chosen. */
type ChunkSizeMode = "default" | "large" | "custom";

// The graphrag-toolkit SentenceSplitter defaults (256 tokens, 25 overlap) apply
// when extractionConfig carries no chunk size. The backend owns them; the
// overlap is here only to describe it and to validate custom values against it.
const TOOLKIT_CHUNK_OVERLAP = 25;
// A 256-token chunk holds far fewer characters of Japanese than of English, so
// a table that fits one English chunk is cut mid-row, and sometimes mid-number,
// in Japanese (#1286). 1024 keeps such a table in one chunk.
const LARGE_CHUNK_SIZE = 1024;
// ExtractionConfig's @range on chunkSize and chunkOverlap is 0-4096 and 0-512,
// where 0 means "use the toolkit default". Custom accepts 1 and up, because the
// Default option is how a user asks for the toolkit default.
const MAX_CHUNK_SIZE = 4096;
const MAX_CHUNK_OVERLAP = 512;

const CHUNK_SIZE_OPTIONS: {
  value: ChunkSizeMode;
  label: AppMessage;
  description: AppMessage;
}[] = [
  {
    value: "default",
    label: "Default (256 tokens)",
    description: "Suits prose.",
  },
  {
    value: "large",
    label: "Large, for tabular or CJK text (1024 tokens)",
    description: "Keeps a full table row and its header in one chunk.",
  },
  {
    value: "custom",
    label: "Custom",
    description: "Set the chunk size and overlap in tokens.",
  },
];

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
  chunkSize?: number;
  chunkOverlap?: number;
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
    ...chunkingFields(model),
  };
}

/**
 * The chunk fields of `extractionConfig`. "Default" sends neither field, so a
 * source that never touches the control stores the same config as before.
 * Custom values are validated by `validateCustomChunking` before submit.
 */
function chunkingFields(model: Model): {
  chunkSize?: number;
  chunkOverlap?: number;
} {
  switch (model.chunkSizeMode) {
    case "default":
      return {};
    case "large":
      return { chunkSize: LARGE_CHUNK_SIZE };
    case "custom": {
      const overlap = model.chunkOverlap.trim();
      return {
        chunkSize: Number(model.chunkSize.trim()),
        ...(overlap ? { chunkOverlap: Number(overlap) } : {}),
      };
    }
  }
}

/** A whole number in [min, max], or null. Rejects "1e3", "10.5" and "-1". */
function parseTokenCount(raw: string, min: number, max: number): number | null {
  const value = raw.trim();
  if (!/^\d+$/.test(value)) return null;
  const n = Number(value);
  return n >= min && n <= max ? n : null;
}

/**
 * Validate the custom chunk size and overlap. An empty overlap means the
 * toolkit's 25 tokens, and the splitter needs the overlap to be smaller than
 * the chunk, so the size is checked against whichever overlap will apply.
 */
function validateCustomChunking(
  chunkSize: string,
  chunkOverlap: string,
  t: AppTranslate,
): Pick<ValidationErrors, "chunkSize" | "chunkOverlap"> {
  const errs: Pick<ValidationErrors, "chunkSize" | "chunkOverlap"> = {};
  const size = parseTokenCount(chunkSize, 1, MAX_CHUNK_SIZE);
  if (size === null) {
    errs.chunkSize = t("Enter a whole number from 1 to {max}.", {
      max: MAX_CHUNK_SIZE,
    });
  }
  let overlap = TOOLKIT_CHUNK_OVERLAP;
  if (chunkOverlap.trim()) {
    const parsed = parseTokenCount(chunkOverlap, 1, MAX_CHUNK_OVERLAP);
    if (parsed === null) {
      errs.chunkOverlap = t(
        "Enter a whole number from 1 to {max}, or leave it empty.",
        { max: MAX_CHUNK_OVERLAP },
      );
      return errs;
    }
    overlap = parsed;
  }
  if (size !== null && overlap >= size) {
    errs.chunkSize = t("Must be larger than the overlap ({overlap} tokens).", {
      overlap,
    });
  }
  return errs;
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
function vocabularyWarning(
  labels: string[],
  t: AppTranslate,
): string | undefined {
  const rewritten = labelsRewrittenOnIngest(labels);
  const parts: string[] = [];
  if (labels.length > RECOMMENDED_MAX_LABELS) {
    parts.push(
      t(
        "{count} classes. Long lists measurably reduce how closely the extractor follows them.",
        { count: labels.length },
      ),
    );
  }
  if (rewritten.length > 0) {
    // List every affected label, not a truncated sample: this warning exists to
    // tell users which of their labels will be renamed on ingest, and hiding some
    // behind "and N more" defeats it. The set is naturally small — only labels
    // whose recorded form differs from what was typed.
    const shown = rewritten
      .map((r) =>
        t("“{typed}” as “{stored}”", {
          typed: r.typed,
          stored: r.stored,
        }),
      )
      .join(", ");
    parts.push(t("Extraction will record {items}.", { items: shown }));
  }
  return parts.length > 0 ? parts.join(" ") : undefined;
}

/**
 * Same idea as {@link vocabularyWarning}, for topics. Topics keep their
 * capitalisation, but an underscore still becomes a space and a trailing full stop
 * is dropped, so a supplied name can still be recorded differently. Surfacing the
 * specific names beats a general sentence nobody reads.
 */
function topicWarning(topics: string[], t: AppTranslate): string | undefined {
  const rewritten = topicsRewrittenOnIngest(topics);
  if (rewritten.length === 0) return undefined;
  const shown = rewritten
    .slice(0, 3)
    .map((r) =>
      t("“{typed}” as “{stored}”", {
        typed: r.typed,
        stored: r.stored,
      }),
    )
    .join(", ");
  const more =
    rewritten.length > 3
      ? t(", and {count} more", { count: rewritten.length - 3 })
      : "";
  return t("Extraction will record {items}{more}.", {
    items: shown,
    more,
  });
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
  databricksName: "",
  databricksWorkspaceHostname: "",
  databricksHttpPath: "",
  databricksCatalog: "",
  databricksSchemaName: "",
  databricksSecretArn: "",
  databricksCrossAccountRoleArn: "",
  databricksTableFilter: "",
  databricksTableExcludeFilter: "",
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
  chunkSizeMode: "default",
  chunkSize: "",
  chunkOverlap: "",
};

// ── Validation ────────────────────────────────────────────────────────────────

/**
 * Validate cross-account IAM role ARN format.
 * Backend IAM policy restricts AssumeRole to roles matching:
 *   arn:aws:iam::*:role/{prefix}-{env}-datasource-access-*
 * Default prefix is "coa", default env is "dev", so pattern becomes:
 *   arn:aws:iam::ACCOUNT:role/coa-dev-datasource-access-SUFFIX
 */
function validateCrossAccountRoleArn(
  arn: string,
  t: AppTranslate,
): string | undefined {
  if (!arn.trim()) return undefined; // optional field
  const roleArnPattern =
    /^arn:aws:iam::\d{12}:role\/[a-zA-Z0-9_+=,.@-]+-datasource-access-[a-zA-Z0-9_+=,.@-]+$/;
  if (!roleArnPattern.test(arn.trim())) {
    return t(
      "Role ARN must match pattern: arn:aws:iam::ACCOUNT:role/{prefix}-datasource-access-*",
    );
  }
  return undefined;
}

/** Recover the deployment's resource prefix from the namespace's ExternalId.
 *
 * The platform derives that ExternalId as `{RESOURCE_PREFIX}{namespaceId}`, so
 * stripping the namespace id off the end leaves the literal string a
 * datasource-access role's name must start with. Nothing else the API returns
 * carries it, so deriving it here is what lets the form name the required prefix
 * rather than leaving the real rule to a server error.
 *
 * Returns `undefined` whenever the derivation cannot be trusted — still loading,
 * no ExternalId, or a value not ending with the namespace id. Callers fall back
 * to the shape check: a prefix that could not be derived must not block a submit.
 */
function deriveResourcePrefix(
  externalId: string | undefined,
  namespaceId: string | undefined,
): string | undefined {
  if (!externalId || !namespaceId) return undefined;
  if (!externalId.endsWith(namespaceId)) return undefined;
  const prefix = externalId.slice(0, externalId.length - namespaceId.length);
  return prefix || undefined;
}

/** The role-name segment the platform's assume grant is scoped to. */
const DATASOURCE_ACCESS_SEGMENT = "datasource-access-";

/** Word the role-name rule for the customer: the deployment's real prefix when
 *  it could be derived, prose when it could not. Shared by every form that states
 *  the rule, because the API enforces the reserved-name check only on the
 *  Databricks path — a Glue or JDBC steward who takes a hardcoded
 *  `{prefix}-datasource-access-*` literally passes the form and fails at first
 *  scan with an opaque AccessDenied. */
function requiredRoleNamePrefix(
  resourcePrefix: string | undefined,
  t: AppTranslate,
): string {
  return resourcePrefix
    ? `'${resourcePrefix}${DATASOURCE_ACCESS_SEGMENT}'`
    : t("this deployment's resource prefix followed by '{segment}'", {
        segment: DATASOURCE_ACCESS_SEGMENT,
      });
}

/**
 * Same shape check, but for a form where the role ARN is `@required`, and
 * against the deployment's real prefix when it could be derived.
 *
 * A Databricks source has no direct-read fallback — the connector reaches the
 * credential only by assuming this role — so an empty value is an error here
 * rather than "no cross-account access requested", as it means on the other forms.
 */
function validateRequiredCrossAccountRoleArn(
  arn: string,
  resourcePrefix: string | undefined,
  t: AppTranslate,
): string | undefined {
  const trimmed = arn.trim();
  if (!trimmed)
    return t(
      "Credential access role ARN is required, and the role's name must start with {prefix} — the assume grant is scoped to that prefix.",
      { prefix: requiredRoleNamePrefix(resourcePrefix, t) },
    );

  const shapeError = validateCrossAccountRoleArn(trimmed, t);
  if (shapeError) return shapeError;
  // Without a derived prefix the shape check is all the form can assert; the API
  // applies the exact rule either way.
  if (!resourcePrefix) return undefined;

  const roleName = trimmed.slice(trimmed.lastIndexOf("/") + 1);
  const requiredStart = `${resourcePrefix}${DATASOURCE_ACCESS_SEGMENT}`;
  if (!roleName.startsWith(requiredStart)) {
    return t(
      "Role name must start with '{prefix}'. This deployment's sts:AssumeRole grant is scoped to that prefix, so a role named anything else is denied however it is configured. The role's account is unconstrained.",
      { prefix: requiredStart },
    );
  }
  return undefined;
}

// Mirror the Smithy patterns: these values reach a `;`-delimited JDBC property list
// or interpolated information_schema SQL, and the server is patterned accordingly.
const DATABRICKS_WORKSPACE_HOSTNAME_RE =
  /^[a-z0-9][a-z0-9.-]*\.(cloud\.databricks\.com|azuredatabricks\.net|gcp\.databricks\.com)$/;
const DATABRICKS_HTTP_PATH_RE =
  /^\/sql\/1\.0\/(warehouses|endpoints)\/[a-zA-Z0-9]+$/;
// Unity Catalog catalog and schema are both plain SQL identifiers.
const DATABRICKS_IDENTIFIER_RE = /^[a-zA-Z_][a-zA-Z0-9_]*$/;
const SECRET_ARN_RE =
  /^arn:(aws|aws-us-gov):secretsmanager:[a-z0-9-]+:\d{12}:secret:.+$/;

/**
 * Validate a connector Lambda function ARN.
 * Mirrors the LambdaFunctionArn shape the backend accepts: a full ARN,
 * optionally qualified with a version, an alias, or $LATEST. Lambda's partial
 * ARN and name-only forms are rejected — the connector runs in the customer's
 * account, so an unqualified name would resolve against this deployment's
 * account instead.
 */
function validateLambdaFunctionArn(
  arn: string,
  t: AppTranslate,
): string | undefined {
  if (!arn.trim()) return undefined; // callers decide whether the field is required
  const lambdaArnPattern =
    /^arn:aws[a-z-]*:lambda:[a-z0-9-]+:\d{12}:function:[a-zA-Z0-9_-]+(:(\$LATEST|[a-zA-Z0-9_-]+))?$/;
  if (!lambdaArnPattern.test(arn.trim())) {
    return t(
      "Must be a full Lambda function ARN: arn:aws:lambda:REGION:ACCOUNT:function:NAME",
    );
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
  databricksName?: string;
  databricksWorkspaceHostname?: string;
  databricksHttpPath?: string;
  databricksCatalog?: string;
  databricksSchemaName?: string;
  databricksSecretArn?: string;
  databricksCrossAccountRoleArn?: string;
  docUploadName?: string;
  docFiles?: string;
  docS3Name?: string;
  docS3BucketArn?: string;
  preferredEntityClassifications?: string;
  preferredTopics?: string;
  chunkSize?: string;
  chunkOverlap?: string;
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
    case "DATABRICKS_SQL_WAREHOUSE":
      return model.databricksName;
    case "DOCUMENTS":
      return docTab === "upload" ? model.docUploadName : model.docS3Name;
  }
}

function validateStep2(
  model: Model,
  docTab: "upload" | "s3",
  t: AppTranslate,
  // Only the Databricks arm uses it; Glue and JDBC keep a shape-only check.
  resourcePrefix?: string,
): ValidationErrors {
  const errs: ValidationErrors = {};
  if (model.sourceKind === "GLUE_DATABASE") {
    if (!model.dbName.trim()) errs.dbName = t("Name is required.");
    if (!model.glueCatalogId.trim())
      errs.glueCatalogId = t("Catalog ID is required.");
    if (!model.glueDatabaseName.trim())
      errs.glueDatabaseName = t("Database name is required.");
    const roleErr = validateCrossAccountRoleArn(
      model.glueCrossAccountRoleArn,
      t,
    );
    if (roleErr) errs.glueCrossAccountRoleArn = roleErr;
    // A Redshift-executed Glue source must name its workgroup — the
    // backend cannot infer which Serverless workgroup to use.
    if (
      model.glueExecutionEngine === "REDSHIFT" &&
      !model.glueRedshiftWorkgroup.trim()
    )
      errs.glueRedshiftWorkgroup = t(
        "Redshift workgroup is required when the execution engine is Redshift.",
      );
  } else if (model.sourceKind === "JDBC_DATABASE") {
    if (!model.jdbcName.trim()) errs.jdbcName = t("Name is required.");
    if (!model.dbEngine) errs.dbEngine = t("Select a database engine.");
    if (!model.dbHost.trim()) errs.dbHost = t("Host is required.");
    if (!model.dbPort.trim()) errs.dbPort = t("Port is required.");
    if (!model.dbDatabaseName.trim())
      errs.dbDatabaseName = t("Database name is required.");
    if (!model.dbSecretArn.trim())
      errs.dbSecretArn = t("Secrets Manager ARN is required.");
    const roleErr = validateCrossAccountRoleArn(
      model.jdbcCrossAccountRoleArn,
      t,
    );
    if (roleErr) errs.jdbcCrossAccountRoleArn = roleErr;
  } else if (model.sourceKind === "CUSTOM_CONNECTOR") {
    if (!model.customConnectorName.trim())
      errs.customConnectorName = t("Name is required.");
    if (!model.customConnectorFunctionArn.trim())
      errs.customConnectorFunctionArn = t(
        "Connector function ARN is required.",
      );
    else {
      const arnErr = validateLambdaFunctionArn(
        model.customConnectorFunctionArn,
        t,
      );
      if (arnErr) errs.customConnectorFunctionArn = arnErr;
    }
    if (!model.customConnectorDatabaseName.trim())
      errs.customConnectorDatabaseName = t("Database name is required.");
  } else if (model.sourceKind === "DATABRICKS_SQL_WAREHOUSE") {
    if (!model.databricksName.trim())
      errs.databricksName = t("Name is required.");
    if (!model.databricksWorkspaceHostname.trim())
      errs.databricksWorkspaceHostname = t("Workspace hostname is required.");
    else if (
      !DATABRICKS_WORKSPACE_HOSTNAME_RE.test(
        model.databricksWorkspaceHostname.trim(),
      )
    )
      errs.databricksWorkspaceHostname = t(
        "Must be a lowercase Databricks workspace host ending in cloud.databricks.com, azuredatabricks.net, or gcp.databricks.com.",
      );
    if (!model.databricksHttpPath.trim())
      errs.databricksHttpPath = t("HTTP path is required.");
    else if (!DATABRICKS_HTTP_PATH_RE.test(model.databricksHttpPath.trim()))
      errs.databricksHttpPath = t(
        "Must be the warehouse's HTTP path: /sql/1.0/warehouses/ID (or /sql/1.0/endpoints/ID).",
      );
    if (!model.databricksCatalog.trim())
      errs.databricksCatalog = t("Unity Catalog catalog is required.");
    else if (!DATABRICKS_IDENTIFIER_RE.test(model.databricksCatalog.trim()))
      errs.databricksCatalog = t(
        "Must be a SQL identifier: a letter or underscore, then letters, digits, or underscores.",
      );
    if (!model.databricksSchemaName.trim())
      errs.databricksSchemaName = t("Unity Catalog schema is required.");
    else if (!DATABRICKS_IDENTIFIER_RE.test(model.databricksSchemaName.trim()))
      errs.databricksSchemaName = t(
        "Must be a SQL identifier: a letter or underscore, then letters, digits, or underscores.",
      );
    if (!model.databricksSecretArn.trim())
      errs.databricksSecretArn = t("Credential secret ARN is required.");
    else if (!SECRET_ARN_RE.test(model.databricksSecretArn.trim()))
      errs.databricksSecretArn = t(
        "Must be a Secrets Manager secret ARN: arn:aws:secretsmanager:REGION:ACCOUNT:secret:NAME",
      );
    const roleErr = validateRequiredCrossAccountRoleArn(
      model.databricksCrossAccountRoleArn,
      resourcePrefix,
      t,
    );
    if (roleErr) errs.databricksCrossAccountRoleArn = roleErr;
  } else {
    // DOCUMENTS
    if (docTab === "upload") {
      if (!model.docUploadName.trim())
        errs.docUploadName = t("Name is required.");
      if (model.docUploadFiles.length === 0)
        errs.docFiles = t("Select at least one file.");
      else if (model.docUploadFiles.length > MAX_UPLOAD_FILES)
        errs.docFiles = t("Maximum {count} files.", {
          count: MAX_UPLOAD_FILES,
        });
      else {
        const bad = model.docUploadFiles.filter(
          (f) => !SUPPORTED_UPLOAD_CONTENT_TYPES.has(resolveContentType(f)),
        );
        if (bad.length > 0)
          errs.docFiles = t("Unsupported: {files}", {
            files: bad.map((f) => f.name).join(", "),
          });
      }
    } else {
      if (!model.docS3Name.trim()) errs.docS3Name = t("Name is required.");
      if (!model.docS3BucketArn.trim())
        errs.docS3BucketArn = t("Bucket ARN is required.");
      else if (!S3_ARN_RE.test(model.docS3BucketArn.trim()))
        errs.docS3BucketArn = t("Must be a valid S3 bucket ARN.");
      const prefixErrs: Record<number, string> = {};
      model.docS3Prefixes.forEach(({ id, value }) => {
        if (!value.trim()) return;
        const e = validateS3Prefix(value, t);
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
    if (model.chunkSizeMode === "custom") {
      Object.assign(
        errs,
        validateCustomChunking(model.chunkSize, model.chunkOverlap, t),
      );
    }
  }
  if (Object.keys(errs).length > 0) errs.step1 = t("Fix the errors above.");
  return errs;
}

// ── Component ─────────────────────────────────────────────────────────────────

export const ConnectSource: React.FC = () => {
  const { t } = useAppI18n();
  const { namespaceId } = useParams<{ namespaceId: string }>();
  // The ExternalId shown for cross-account roles is derived server-side from the
  // namespace, so it comes from the namespace detail rather than being typed in.
  const {
    data: namespaceData,
    isLoading: namespaceLoading,
    isError: namespaceLoadFailed,
  } = useGetNamespace(namespaceId ?? "");
  const datasourceExternalId = namespaceData?.namespace?.datasourceExternalId;
  const resourcePrefix = useMemo(
    () => deriveResourcePrefix(datasourceExternalId, namespaceId),
    [datasourceExternalId, namespaceId],
  );
  const navigate = useNavigate();

  const { name: nsName } = useNamespace(namespaceId);
  useBreadcrumbTrail([
    { text: nsName, href: `/namespaces/${namespaceId}`, namespaceId },
    {
      text: t("Sources"),
      href: `/namespaces/${namespaceId}/sources`,
      namespaceId,
    },
    { text: t("Connect") },
  ]);

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
    model.sourceKind === "CUSTOM_CONNECTOR" ||
    model.sourceKind === "DATABRICKS_SQL_WAREHOUSE";

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
    const step2Errs = validateStep2(model, docTab, t, resourcePrefix);
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

    if (model.sourceKind === "DATABRICKS_SQL_WAREHOUSE") {
      createSource({
        namespaceId: namespaceId!,
        body: {
          sourceType: SourceType.DATABASE,
          databaseSource: {
            name: model.databricksName.trim(),
            metadataEnrichmentEnabled: model.metadataEnrichmentEnabled,
            // One configuration only: the backend rejects a payload carrying more
            // than one, since the configuration present selects the sub-type.
            databricksSqlWarehouseConfiguration: {
              workspaceHostname: model.databricksWorkspaceHostname.trim(),
              httpPath: model.databricksHttpPath.trim(),
              databricksCatalog: model.databricksCatalog.trim(),
              databaseName: model.databricksSchemaName.trim(),
              credentialSecretArn: model.databricksSecretArn.trim(),
              crossAccountRoleArn: model.databricksCrossAccountRoleArn.trim(),
              tableFilter: model.databricksTableFilter.trim() || undefined,
              tableExcludeFilter:
                model.databricksTableExcludeFilter.trim() || undefined,
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
    // Which call threw decides which alert reports the failure, so record it.
    // The error's class decides nothing here: the upload-URL request throws the
    // same service exception the create call does, and that mutation's error
    // state is never rendered, so a failure there cleared the progress bar and
    // showed nothing at all.
    let createStarted = false;
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
      createStarted = true;
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
      // The create call has its own alert on this step, fed by createError, which
      // is how every other source kind on this page reports a failed create.
      // Everything before it reports here, so one failure always yields one message.
      if (!createStarted) {
        setUploadError(
          extractErrorMessage(err, t("Upload failed. Please try again.")),
        );
      }
    }
  };

  // ── Steps ──────────────────────────────────────────────────────────
  const step2Errs = useMemo(
    () =>
      showErrors[1] ? validateStep2(model, docTab, t, resourcePrefix) : {},
    [showErrors, model, docTab, t, resourcePrefix],
  );
  const chunkSizeOptions = useMemo(
    () =>
      CHUNK_SIZE_OPTIONS.map((option) => ({
        ...option,
        label: t(option.label),
        description: t(option.description),
      })),
    [t],
  );

  const steps: WizardProps.Step[] = useMemo(() => {
    // ── Step 1: Choose source type (3 tiles) ──────────────────────────
    const step1: WizardProps.Step = {
      title: t("Choose source type"),
      description: t("Select the type of source you want to connect."),
      content: (
        <Container header={<Header variant="h2">{t("Source type")}</Header>}>
          <Tiles
            value={model.sourceKind}
            onChange={({ detail }) => {
              if (isSourceKind(detail.value))
                setField("sourceKind", detail.value);
            }}
            items={[
              {
                value: "GLUE_DATABASE",
                label: t(SOURCE_KIND_LABELS.GLUE_DATABASE),
                description: t(
                  "Connect to an existing AWS Glue Data Catalog database. Covers S3/Iceberg tables, DynamoDB, and Athena-federated JDBC sources.",
                ),
              },
              {
                value: "JDBC_DATABASE",
                label: t(SOURCE_KIND_LABELS.JDBC_DATABASE),
                description: t(
                  "Connect directly to a relational database. Schema is discovered from information_schema. Supported engines: {engines}.",
                  { engines: ENGINE_LABELS },
                ),
              },
              {
                value: "CUSTOM_CONNECTOR",
                label: t(SOURCE_KIND_LABELS.CUSTOM_CONNECTOR),
                description: t(
                  "Bring your own data source: register an Athena Query Federation SDK connector you built and deployed as a Lambda in your own AWS account. Schema and rows are read through Athena against that connector.",
                ),
              },
              {
                value: "DATABRICKS_SQL_WAREHOUSE",
                label: t(SOURCE_KIND_LABELS.DATABRICKS_SQL_WAREHOUSE),
                description: t(
                  "Connect one Unity Catalog schema on a Databricks SQL Warehouse. Nothing to deploy — Context Ontology Accelerator operates the connector; you supply the warehouse, the credential secret, and the role that reads it.",
                ),
              },
              {
                value: "DOCUMENTS",
                label: t(SOURCE_KIND_LABELS.DOCUMENTS),
                description: t(
                  "Ingest unstructured documents from an S3 bucket or by uploading files directly.",
                ),
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
        title: t("Configure connection"),
        description: t("Select the Glue Data Catalog database to connect."),
        errorText: step2Errs.step1,
        content: (
          <SpaceBetween size="l">
            <Container
              header={<Header variant="h2">{t("Source details")}</Header>}
            >
              <SpaceBetween size="l">
                <FormField
                  label={
                    <>
                      {t("Source name")}{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description={t(
                    "A unique name for this source within the namespace.",
                  )}
                  errorText={step2Errs.dbName}
                >
                  <Input
                    value={model.dbName}
                    onChange={({ detail }) => setField("dbName", detail.value)}
                    placeholder={t("My Glue Database")}
                  />
                </FormField>
              </SpaceBetween>
            </Container>
            <Container
              header={<Header variant="h2">{t("Glue database")}</Header>}
            >
              <SpaceBetween size="l">
                <FormField
                  label={
                    <>
                      {t("Catalog ID (AWS account)")}{" "}
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
                      {t("Region")}{" "}
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
                      {t("Database name")}{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description={t(
                    "Choose a Glue Data Catalog database to connect. All tables within the database will be scanned.",
                  )}
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
              headerText={t("Advanced configuration")}
              variant="container"
            >
              <SpaceBetween size="l">
                <FormField
                  label={t("Table filter")}
                  description={t(
                    "Optional Glue expression (a regular expression, e.g. orders|customers) to include specific tables.",
                  )}
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
                  label={t("Table exclude filter")}
                  description={t(
                    "Optional glob(s) (* ? [seq]) to exclude specific tables. Applied after the include filter. Separate multiple with | or ,.",
                  )}
                >
                  <Input
                    value={model.glueTableExcludeFilter}
                    onChange={({ detail }) =>
                      setField("glueTableExcludeFilter", detail.value)
                    }
                    placeholder="tmp_*|staging_*"
                  />
                </FormField>
                <FormField
                  label={t("Execution engine")}
                  description={t(
                    "How this Glue/Iceberg source's queries run at serve time. Redshift runs them via Redshift Serverless (awsdatacatalog auto-mount) instead of Athena. This does NOT connect to a Redshift database — to query a Redshift cluster's own tables, onboard a JDBC source instead.",
                  )}
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
                    options={GLUE_EXECUTION_ENGINE_OPTIONS.map((option) => ({
                      ...option,
                      label: t(option.label),
                      description: t(option.description),
                    }))}
                  />
                </FormField>
                {model.glueExecutionEngine === "REDSHIFT" && (
                  <FormField
                    label={
                      <>
                        {t("Redshift workgroup")}{" "}
                        <Box variant="span" color="text-status-error">
                          *
                        </Box>
                      </>
                    }
                    description={t(
                      "The Amazon Redshift Serverless workgroup that executes this source's queries.",
                    )}
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
                    label={t("Athena DataCatalog name")}
                    description={t(
                      "Optional. Required for JDBC-backed Glue databases to enable federated queries.",
                    )}
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
                  label={t("Cross-account role ARN")}
                  description={t(
                    "Optional. For a Glue catalog in another account, the IAM role Context Ontology Accelerator assumes to read catalog metadata. Its name must start with {prefix}, and its trust policy must require the External ID below.",
                    { prefix: requiredRoleNamePrefix(resourcePrefix, t) },
                  )}
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
                  loadFailed={namespaceLoadFailed}
                />
              </SpaceBetween>
            </ExpandableSection>
          </SpaceBetween>
        ),
      };
    } else if (model.sourceKind === "JDBC_DATABASE") {
      step2 = {
        title: t("Configure connection"),
        description: t("Provide connection details for your JDBC database."),
        errorText: step2Errs.step1,
        content: (
          <SpaceBetween size="l">
            <Container
              header={<Header variant="h2">{t("Source details")}</Header>}
            >
              <FormField
                label={
                  <>
                    {t("Source name")}{" "}
                    <Box variant="span" color="text-status-error">
                      *
                    </Box>
                  </>
                }
                description={t(
                  "A unique name for this source within the namespace.",
                )}
                errorText={step2Errs.jdbcName}
              >
                <Input
                  value={model.jdbcName}
                  onChange={({ detail }) => setField("jdbcName", detail.value)}
                  placeholder={t("My JDBC Database")}
                />
              </FormField>
            </Container>
            <Container
              header={<Header variant="h2">{t("Connection settings")}</Header>}
            >
              <SpaceBetween size="l">
                <FormField
                  label={
                    <>
                      {t("Database engine")}{" "}
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
                    placeholder={t("Select an engine")}
                  />
                </FormField>
                <FormField
                  label={
                    <>
                      {t("Host")}{" "}
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
                      {t("Port")}{" "}
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
                      {t("Database name")}{" "}
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
                      {t("Credential secret ARN")}{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description={t(
                    "AWS Secrets Manager secret containing the database username and password.",
                  )}
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
              headerText={t("Advanced configuration")}
              variant="container"
            >
              <SpaceBetween size="l">
                <FormField
                  label={t("Schema filter")}
                  description={t(
                    "Optional glob(s) (* ? [seq]) to include specific schemas. Separate multiple with | or ,.",
                  )}
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
                  label={t("Schema exclude filter")}
                  description={t(
                    "Optional glob(s) (* ? [seq]) to exclude specific schemas, in addition to the engine's system schemas (always excluded). Separate multiple with | or ,.",
                  )}
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
                  label={t("Table filter")}
                  description={t(
                    "Optional glob(s) (* ? [seq]) to include specific tables. Separate multiple with | or ,.",
                  )}
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
                  label={t("Table exclude filter")}
                  description={t(
                    "Optional glob(s) (* ? [seq]) to exclude specific tables. Separate multiple with | or ,.",
                  )}
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
                      label={t("Warehouse")}
                      description={t(
                        "Snowflake virtual warehouse for discovery queries.",
                      )}
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
                      label={t("Role")}
                      description={t("Optional Snowflake RBAC role to assume.")}
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
                  label={t("Cross-account role ARN")}
                  description={t(
                    "Optional. For a credential secret in another account, the IAM role Context Ontology Accelerator assumes to read it. Its name must start with {prefix}, and its trust policy must require the External ID below.",
                    { prefix: requiredRoleNamePrefix(resourcePrefix, t) },
                  )}
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
                  loadFailed={namespaceLoadFailed}
                />
              </SpaceBetween>
            </ExpandableSection>
          </SpaceBetween>
        ),
      };
    } else if (model.sourceKind === "CUSTOM_CONNECTOR") {
      step2 = {
        title: t("Configure connection"),
        description: t(
          "Point at the Athena Query Federation connector you deployed.",
        ),
        errorText: step2Errs.step1,
        content: (
          <SpaceBetween size="l">
            <Container
              header={<Header variant="h2">{t("Source details")}</Header>}
            >
              <FormField
                label={
                  <>
                    {t("Source name")}{" "}
                    <Box variant="span" color="text-status-error">
                      *
                    </Box>
                  </>
                }
                description={t(
                  "A unique name for this source within the namespace.",
                )}
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
            <Container header={<Header variant="h2">{t("Connector")}</Header>}>
              <SpaceBetween size="l">
                <FormField
                  label={
                    <>
                      {t("Connector function ARN")}{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description={t(
                    "The connector Lambda. It serves both metadata and record requests. It must live in the same AWS Region as Context Ontology Accelerator; a connector in another Region is rejected, because Athena can only invoke a data source connector co-located with the query.",
                  )}
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
                      {t("Database name")}{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description={t(
                    "The single database inside the connector's catalog that this source exposes. A connector serving several databases is onboarded once per database.",
                  )}
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
              headerText={t("Advanced configuration")}
              variant="container"
            >
              <SpaceBetween size="l">
                <FormField
                  label={t("Table filter")}
                  description={t(
                    "Optional glob(s) (* ? [seq]) to include specific tables. Separate multiple with | or ,.",
                  )}
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
                  label={t("Table exclude filter")}
                  description={t(
                    "Optional glob(s) (* ? [seq]) to exclude specific tables. Applied after the include filter. Separate multiple with | or ,.",
                  )}
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
    } else if (model.sourceKind === "DATABRICKS_SQL_WAREHOUSE") {
      step2 = {
        title: t("Configure connection"),
        description: t(
          "Name the warehouse to read and the credential Context Ontology Accelerator may read it with.",
        ),
        errorText: step2Errs.step1,
        content: (
          <SpaceBetween size="l">
            <Container
              header={<Header variant="h2">{t("Source details")}</Header>}
            >
              <FormField
                label={
                  <>
                    {t("Source name")}{" "}
                    <Box variant="span" color="text-status-error">
                      *
                    </Box>
                  </>
                }
                description={t(
                  "A unique name for this source within the namespace.",
                )}
                errorText={step2Errs.databricksName}
              >
                <Input
                  value={model.databricksName}
                  onChange={({ detail }) =>
                    setField("databricksName", detail.value)
                  }
                  placeholder={t("My Databricks Warehouse")}
                />
              </FormField>
            </Container>
            <Container
              header={<Header variant="h2">{t("SQL Warehouse")}</Header>}
            >
              <SpaceBetween size="l">
                <FormField
                  label={
                    <>
                      {t("Workspace hostname")}{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description={t(
                    "The workspace's server hostname, without a scheme or path. AWS, Azure, and GCP workspaces are all accepted.",
                  )}
                  constraintText={t(
                    "A PrivateLink-only workspace is not reachable: the connector runs outside your VPC.",
                  )}
                  errorText={step2Errs.databricksWorkspaceHostname}
                >
                  <Input
                    value={model.databricksWorkspaceHostname}
                    onChange={({ detail }) =>
                      setField("databricksWorkspaceHostname", detail.value)
                    }
                    placeholder="dbc-a1b2345c-d6e7.cloud.databricks.com"
                  />
                </FormField>
                <FormField
                  label={
                    <>
                      {t("HTTP path")}{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description={t(
                    "Copy it from the warehouse's Connection details tab. The Databricks analogue of a port — it selects which warehouse this source queries.",
                  )}
                  constraintText={t(
                    "Keep the warehouse running, or make its auto-stop window match how often you expect questions: a stopped warehouse turns the first query into a resume, seconds on serverless and minutes on classic or pro.",
                  )}
                  errorText={step2Errs.databricksHttpPath}
                >
                  <Input
                    value={model.databricksHttpPath}
                    onChange={({ detail }) =>
                      setField("databricksHttpPath", detail.value)
                    }
                    placeholder="/sql/1.0/warehouses/abc123def456"
                  />
                </FormField>
                <FormField
                  label={
                    <>
                      {t("Unity Catalog catalog")}{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  errorText={step2Errs.databricksCatalog}
                >
                  <Input
                    value={model.databricksCatalog}
                    onChange={({ detail }) =>
                      setField("databricksCatalog", detail.value)
                    }
                    placeholder="main"
                  />
                </FormField>
                <FormField
                  label={
                    <>
                      {t("Unity Catalog schema")}{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description={t(
                    "The single schema this source exposes. A catalog with several schemas is onboarded once per schema.",
                  )}
                  constraintText={t(
                    "Every table must be SELECT-able by this credential. Visibility in information_schema comes from BROWSE or USE SCHEMA, so a table that is discovered is not necessarily one that can be read — and it would fail only when a question reaches it.",
                  )}
                  errorText={step2Errs.databricksSchemaName}
                >
                  <Input
                    value={model.databricksSchemaName}
                    onChange={({ detail }) =>
                      setField("databricksSchemaName", detail.value)
                    }
                    placeholder="sales"
                  />
                </FormField>
              </SpaceBetween>
            </Container>
            <Container
              header={<Header variant="h2">{t("Credential access")}</Header>}
            >
              <SpaceBetween size="l">
                <FormField
                  label={
                    <>
                      {t("Credential secret ARN")}{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description={t(
                    'AWS Secrets Manager secret holding the Databricks credential, in this deployment\'s Region. Its JSON shape selects the authentication mode: {"token": "…"} for a personal access token, {"client_id": "…", "client_secret": "…"} for OAuth machine-to-machine.',
                  )}
                  constraintText={t(
                    "Machine-to-machine is the recommendation: a personal access token carries a user's identity and expires on a schedule you may not control.",
                  )}
                  errorText={step2Errs.databricksSecretArn}
                >
                  <Input
                    value={model.databricksSecretArn}
                    onChange={({ detail }) =>
                      setField("databricksSecretArn", detail.value)
                    }
                    placeholder="arn:aws:secretsmanager:us-east-1:123456789012:secret:databricks-abc123"
                  />
                </FormField>
                <FormField
                  label={
                    <>
                      {t("Credential access role ARN")}{" "}
                      <Box variant="span" color="text-status-error">
                        *
                      </Box>
                    </>
                  }
                  description={t(
                    "Required. The IAM role Context Ontology Accelerator assumes to read the secret above — it holds no Secrets Manager permission of its own, so there is no fallback. The role's name must start with {prefix}, and its trust policy must require the External ID below. Any AWS account, including this one.",
                    { prefix: requiredRoleNamePrefix(resourcePrefix, t) },
                  )}
                  errorText={step2Errs.databricksCrossAccountRoleArn}
                >
                  <Input
                    value={model.databricksCrossAccountRoleArn}
                    onChange={({ detail }) =>
                      setField("databricksCrossAccountRoleArn", detail.value)
                    }
                    placeholder="arn:aws:iam::222222222222:role/coa-dev-datasource-access-databricks"
                  />
                </FormField>
                <CrossAccountExternalIdField
                  externalId={datasourceExternalId}
                  isLoading={namespaceLoading}
                  loadFailed={namespaceLoadFailed}
                />
              </SpaceBetween>
            </Container>
            <ExpandableSection
              headerText={t("Advanced configuration")}
              variant="container"
            >
              <SpaceBetween size="l">
                <FormField
                  label={t("Table filter")}
                  description={t(
                    "Optional glob(s) (* ? [seq]) to include specific tables. Separate multiple with | or ,.",
                  )}
                >
                  <Input
                    value={model.databricksTableFilter}
                    onChange={({ detail }) =>
                      setField("databricksTableFilter", detail.value)
                    }
                    placeholder="orders|customers"
                  />
                </FormField>
                <FormField
                  label={t("Table exclude filter")}
                  description={t(
                    "Optional glob(s) (* ? [seq]) to exclude specific tables. Applied after the include filter. Separate multiple with | or ,.",
                  )}
                >
                  <Input
                    value={model.databricksTableExcludeFilter}
                    onChange={({ detail }) =>
                      setField("databricksTableExcludeFilter", detail.value)
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
        title: t("Configure source"),
        description: t("Upload files or connect an S3 bucket."),
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
                  label: t("Upload files"),
                  content: (
                    <SpaceBetween size="l">
                      <Container
                        header={
                          <Header variant="h2">{t("Source details")}</Header>
                        }
                      >
                        <SpaceBetween size="l">
                          <FormField
                            label={
                              <>
                                {t("Name")}{" "}
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
                                {t("Files")}{" "}
                                <Box variant="span" color="text-status-error">
                                  *
                                </Box>
                              </>
                            }
                            description={t(
                              "PDF, DOCX, TXT, or Markdown. Maximum 100 files.",
                            )}
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
                            />
                          </FormField>
                          {uploadProgress !== null && (
                            <ProgressBar
                              value={uploadProgress}
                              label={t("Uploading files")}
                              description={
                                uploadProgress < 95
                                  ? t("Uploading {count} file(s)…", {
                                      count: model.docUploadFiles.length,
                                    })
                                  : t("Triggering ingestion…")
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
                  label: t("S3 bucket"),
                  content: (
                    <SpaceBetween size="l">
                      <Container
                        header={
                          <Header variant="h2">{t("Source details")}</Header>
                        }
                      >
                        <SpaceBetween size="l">
                          <FormField
                            label={
                              <>
                                {t("Name")}{" "}
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
                                {t("Source bucket ARN")}{" "}
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
                            label={t("S3 prefixes")}
                            description={t(
                              "Leave blank to ingest the entire bucket.",
                            )}
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
                                      ariaLabel={t("S3 prefix {number}", {
                                        number: idx + 1,
                                      })}
                                    />
                                  </FormField>
                                  {model.docS3Prefixes.length > 1 && (
                                    <Box padding={{ top: "xs" }}>
                                      <Button
                                        iconName="close"
                                        variant="icon"
                                        ariaLabel={t("Remove prefix")}
                                        onClick={() => removePrefix(id)}
                                      />
                                    </Box>
                                  )}
                                </SpaceBetween>
                              ))}
                              <Button iconName="add-plus" onClick={addPrefix}>
                                {t("Add prefix")}
                              </Button>
                            </SpaceBetween>
                          </FormField>
                        </SpaceBetween>
                      </Container>
                      <ExpandableSection
                        headerText={t("Cross-account access (optional)")}
                        variant="container"
                      >
                        <FormField label={t("Role ARN")}>
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
              headerText={t("Entity classes and topics (optional)")}
              variant="container"
            >
              <SpaceBetween size="l">
                <FormField
                  label={t("Entity classes")}
                  description={t(
                    "What the extractor calls each thing it finds — a brand, a policy, a material. Each entity is recorded with its class, and those classes become classes in the ontology.",
                  )}
                  constraintText={
                    model.vocabularyMode === "custom"
                      ? t(
                          "Enter one at a time, or paste several separated by commas. {max} characters each; 15–25 classes works best.",
                          { max: MAX_CLASSIFICATION_LENGTH },
                        )
                      : undefined
                  }
                  characterCountText={
                    model.vocabularyMode === "custom"
                      ? t("{count} of {max} classes", {
                          count: model.preferredEntityClassifications.length,
                          max: MAX_VOCABULARY_ENTRIES,
                        })
                      : undefined
                  }
                  warningText={
                    model.vocabularyMode === "custom"
                      ? vocabularyWarning(
                          model.preferredEntityClassifications,
                          t,
                        )
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
                          label: t("Infer from my documents"),
                          description: t(
                            "Recommended. Classes are derived from a sample of your documents at ingest.",
                          ),
                        },
                        {
                          value: "custom",
                          label: t("Define my own"),
                          description: t(
                            "Your classes are preferred, and a new one is created when nothing on your list fits. No extra classes are derived from your documents.",
                          ),
                        },
                      ]}
                    />
                    {model.vocabularyMode === "custom" && (
                      <TokenInput
                        value={model.preferredEntityClassifications}
                        onChange={(next) =>
                          setField("preferredEntityClassifications", next)
                        }
                        placeholder={t("e.g. Loss Ratio, Claim Handler")}
                        ariaLabel={t("Entity class labels")}
                        itemLabelPlural="classes"
                        validate={(candidate, existing) =>
                          validateClassification(candidate, existing, t)
                        }
                      />
                    )}
                  </SpaceBetween>
                </FormField>
                <FormField
                  label={t("Topics")}
                  description={t(
                    "What a passage is about, rather than what a thing is. Topics group related passages together and drive topic-based retrieval.",
                  )}
                  constraintText={
                    model.topicMode === "custom"
                      ? t(
                          "Enter one at a time, or paste several separated by commas. Phrases are fine, and your capitalisation is kept. {max} characters each.",
                          { max: MAX_TOPIC_LENGTH },
                        )
                      : undefined
                  }
                  characterCountText={
                    model.topicMode === "custom"
                      ? t("{count} of {max} topics", {
                          count: model.preferredTopics.length,
                          max: MAX_VOCABULARY_ENTRIES,
                        })
                      : undefined
                  }
                  warningText={
                    model.topicMode === "custom"
                      ? topicWarning(model.preferredTopics, t)
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
                          label: t("Automatic"),
                          description: t(
                            "Recommended. Topics are extracted from your documents.",
                          ),
                        },
                        {
                          value: "custom",
                          label: t("Define my own"),
                          description: t(
                            "Topics are still extracted from your documents; your names are used where one matches. Expect topics beyond your list.",
                          ),
                        },
                      ]}
                    />
                    {model.topicMode === "custom" && (
                      <TokenInput
                        value={model.preferredTopics}
                        onChange={(next) => setField("preferredTopics", next)}
                        placeholder={t("e.g. Black Tie Gala, Quiet Luxury")}
                        ariaLabel={t("Preferred topic names")}
                        itemLabelPlural="topics"
                        validate={(candidate, existing) =>
                          validateTopic(candidate, existing, t)
                        }
                      />
                    )}
                  </SpaceBetween>
                </FormField>
              </SpaceBetween>
            </ExpandableSection>
            <ExpandableSection
              headerText={t("Advanced options")}
              variant="container"
            >
              <SpaceBetween size="l">
                <FormField
                  label={t("Chunk size")}
                  description={t(
                    "How much text goes into each unit that is extracted and embedded. A larger chunk keeps a full table row and its header together; raise it for CJK or dense, tabular documents.",
                  )}
                >
                  <Select
                    selectedOption={
                      chunkSizeOptions.find(
                        (o) => o.value === model.chunkSizeMode,
                      ) ?? null
                    }
                    onChange={({ detail }) => {
                      const selected = chunkSizeOptions.find(
                        (o) => o.value === detail.selectedOption.value,
                      );
                      if (selected) setField("chunkSizeMode", selected.value);
                    }}
                    options={chunkSizeOptions}
                  />
                </FormField>
                {model.chunkSizeMode === "custom" && (
                  <>
                    <FormField
                      label={t("Chunk size (tokens)")}
                      constraintText={t("A whole number from 1 to {max}.", {
                        max: MAX_CHUNK_SIZE,
                      })}
                      errorText={step2Errs.chunkSize}
                    >
                      <Input
                        value={model.chunkSize}
                        onChange={({ detail }) =>
                          setField("chunkSize", detail.value)
                        }
                        inputMode="numeric"
                        placeholder={String(LARGE_CHUNK_SIZE)}
                      />
                    </FormField>
                    <FormField
                      label={t("Chunk overlap (tokens)")}
                      constraintText={t(
                        "Optional. Leave empty to use {overlap}. Must be smaller than the chunk size.",
                        { overlap: TOOLKIT_CHUNK_OVERLAP },
                      )}
                      errorText={step2Errs.chunkOverlap}
                    >
                      <Input
                        value={model.chunkOverlap}
                        onChange={({ detail }) =>
                          setField("chunkOverlap", detail.value)
                        }
                        inputMode="numeric"
                        placeholder={String(TOOLKIT_CHUNK_OVERLAP)}
                      />
                    </FormField>
                  </>
                )}
                <FormField
                  description={t("Tracks document versions in Neptune.")}
                >
                  <Toggle
                    checked={model.enableVersioning}
                    onChange={({ detail }) =>
                      setField("enableVersioning", detail.checked)
                    }
                  >
                    {t("Enable versioning")}
                  </Toggle>
                </FormField>
                <FormField
                  description={t(
                    "Deletes previous versions after re-ingestion.",
                  )}
                >
                  <Toggle
                    checked={model.deletePrevVersions}
                    disabled={!model.enableVersioning}
                    onChange={({ detail }) =>
                      setField("deletePrevVersions", detail.checked)
                    }
                  >
                    {t("Delete previous versions on re-ingestion")}
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
      title: t("Configure enrichment"),
      description: t("Choose AI enrichment features."),
      content: (
        <Container
          header={<Header variant="h2">{t("Metadata enrichment")}</Header>}
        >
          <SpaceBetween size="l">
            <FormField
              description={t(
                "When disabled, only technical metadata is captured during the scan. AI-generated descriptions, synonyms, glossary terms, and tags are skipped.",
              )}
            >
              <Toggle
                checked={model.metadataEnrichmentEnabled}
                onChange={({ detail }) =>
                  setField("metadataEnrichmentEnabled", detail.checked)
                }
              >
                {t("Enable AI metadata enrichment")}
              </Toggle>
            </FormField>
            <SpaceBetween size="s">
              <Box variant="h4">{t("Auto-generate descriptions")}</Box>
              <Checkbox checked={model.metadataEnrichmentEnabled} disabled>
                {t(
                  "Generate business-friendly descriptions for tables and columns",
                )}
              </Checkbox>
              <Box variant="small" color="text-body-secondary">
                {t(
                  "1–2 sentence business-friendly description per table and column. Tagged AI_GENERATED.",
                )}
              </Box>
            </SpaceBetween>
            <SpaceBetween size="s">
              <Box variant="h4">{t("Generate synonyms")}</Box>
              <Checkbox checked={model.metadataEnrichmentEnabled} disabled>
                {t(
                  "Generate business-language alternatives for tables and columns",
                )}
              </Checkbox>
              <Box variant="small" color="text-body-secondary">
                {t("2–5 business-language alternatives per table and column.")}
              </Box>
            </SpaceBetween>
            <SpaceBetween size="s">
              <Box variant="h4">{t("Generate glossary terms")}</Box>
              <Checkbox checked={model.metadataEnrichmentEnabled} disabled>
                {t("Map tables and columns to business domain concepts")}
              </Checkbox>
              <Box variant="small" color="text-body-secondary">
                {t(
                  '1–3 business domain concepts (e.g., "Order Management", "Revenue").',
                )}
              </Box>
            </SpaceBetween>
            <SpaceBetween size="s">
              <Box variant="h4">{t("Constraint discovery")}</Box>
              <Checkbox checked={model.metadataEnrichmentEnabled} disabled>
                {t("Enable AI-assisted cross-table relationship inference")}
              </Checkbox>
              <Box variant="small" color="text-body-secondary">
                {t(
                  "AI-inferred relationships are tagged as AI_INFERRED and require steward review before promotion.",
                )}
              </Box>
            </SpaceBetween>
          </SpaceBetween>
        </Container>
      ),
    };

    // ── Step 3/4: Review ──────────────────────────────────────────────
    const reviewStep: WizardProps.Step = {
      title: t("Review and connect"),
      description: t("Review your configuration before connecting."),
      content: (
        <SpaceBetween size="l">
          <Container header={<Header variant="h2">{t("Source type")}</Header>}>
            <KeyValuePairs
              columns={2}
              items={[
                {
                  label: t("Category"),
                  value: t(SOURCE_KIND_LABELS[model.sourceKind]),
                },
                {
                  label: t("Source name"),
                  value: sourceNameFor(model, docTab),
                },
              ]}
            />
          </Container>
          {model.sourceKind === "GLUE_DATABASE" && (
            <Container header={<Header variant="h2">{t("Connection")}</Header>}>
              <KeyValuePairs
                columns={2}
                items={[
                  { label: t("Catalog ID"), value: model.glueCatalogId },
                  { label: t("Region"), value: model.glueRegion },
                  { label: t("Database"), value: model.glueDatabaseName },
                  {
                    label: t("Table filter"),
                    value: model.glueTableFilter || t("(none)"),
                  },
                ]}
              />
            </Container>
          )}
          {model.sourceKind === "JDBC_DATABASE" && (
            <Container header={<Header variant="h2">{t("Connection")}</Header>}>
              <KeyValuePairs
                columns={2}
                items={[
                  {
                    label: t("Engine"),
                    value:
                      ENGINE_OPTIONS.find((o) => o.value === model.dbEngine)
                        ?.label ?? "—",
                  },
                  { label: t("Host"), value: model.dbHost },
                  { label: t("Port"), value: model.dbPort },
                  { label: t("Database name"), value: model.dbDatabaseName },
                  {
                    label: t("Schema filter"),
                    value: model.dbSchemaFilter || t("(none)"),
                  },
                  {
                    label: t("Table filter"),
                    value: model.dbTableFilter || t("(none)"),
                  },
                  {
                    label: t("Authentication"),
                    value: "AWS Secrets Manager",
                  },
                ]}
              />
            </Container>
          )}
          {model.sourceKind === "CUSTOM_CONNECTOR" && (
            <Container header={<Header variant="h2">{t("Connection")}</Header>}>
              <KeyValuePairs
                columns={2}
                items={[
                  {
                    label: t("Connector function ARN"),
                    value: model.customConnectorFunctionArn,
                  },
                  {
                    label: t("Database"),
                    value: model.customConnectorDatabaseName,
                  },
                  {
                    label: t("Table filter"),
                    value: model.customConnectorTableFilter || t("(none)"),
                  },
                ]}
              />
            </Container>
          )}
          {model.sourceKind === "DATABRICKS_SQL_WAREHOUSE" && (
            <Container header={<Header variant="h2">{t("Connection")}</Header>}>
              <KeyValuePairs
                columns={2}
                items={[
                  {
                    label: t("Workspace hostname"),
                    value: model.databricksWorkspaceHostname,
                  },
                  { label: t("HTTP path"), value: model.databricksHttpPath },
                  {
                    label: t("Unity Catalog catalog"),
                    value: model.databricksCatalog,
                  },
                  {
                    label: t("Unity Catalog schema"),
                    value: model.databricksSchemaName,
                  },
                  {
                    label: t("Credential secret ARN"),
                    value: model.databricksSecretArn,
                  },
                  {
                    label: t("Credential access role ARN"),
                    value: model.databricksCrossAccountRoleArn,
                  },
                  {
                    label: t("Table filter"),
                    value: model.databricksTableFilter || t("(none)"),
                  },
                ]}
              />
            </Container>
          )}
          {model.sourceKind === "DOCUMENTS" && (
            <Container header={<Header variant="h2">{t("Source")}</Header>}>
              <KeyValuePairs
                columns={2}
                items={[
                  {
                    label: t("Mode"),
                    value:
                      docTab === "upload" ? t("File upload") : t("S3 bucket"),
                  },
                  ...(docTab === "s3"
                    ? [{ label: t("Bucket ARN"), value: model.docS3BucketArn }]
                    : [
                        {
                          label: t("Files"),
                          value: t("{count} file(s)", {
                            count: model.docUploadFiles.length,
                          }),
                        },
                      ]),
                ]}
              />
            </Container>
          )}
          {isDatabase && (
            <Container
              header={<Header variant="h2">{t("Enrichment settings")}</Header>}
            >
              <KeyValuePairs
                columns={2}
                items={[
                  {
                    label: t("Metadata enrichment"),
                    value: model.metadataEnrichmentEnabled
                      ? t("Enabled")
                      : t("Disabled"),
                  },
                ]}
              />
            </Container>
          )}
          {isDatabase && (
            <Alert type="info" header={t("What happens next")}>
              {model.metadataEnrichmentEnabled
                ? t(
                    "Context Ontology Accelerator will start the scan and enrichment pipeline. The data source status changes to Scanning while technical metadata is discovered, then to Enriching while AI generates business descriptions, synonyms, glossary terms, and tags. Once complete, the status changes to Pending review — you will be notified to review and approve the enriched metadata before it is used for ontology induction.",
                  )
                : t(
                    "Context Ontology Accelerator will scan the source and discover technical metadata only. AI metadata enrichment is disabled, so no business descriptions, synonyms, glossary terms, or tags will be generated. The status changes to Scanning, then directly to Pending review for steward approval.",
                  )}
            </Alert>
          )}
          {uploadError && (
            <Alert type="error" header={t("Upload failed")}>
              {uploadError}
            </Alert>
          )}
          {createError && (
            <Alert type="error" header={t("Failed to connect source")}>
              {/* Registration errors are newline-formatted numbered lists naming the
                  exact sts:ExternalId — render them as written, not on one line. */}
              <div className="coa-preserve-newlines">
                {extractErrorMessage(
                  createError,
                  t("An unexpected error occurred."),
                )}
              </div>
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
    t,
    resourcePrefix,
    datasourceExternalId,
    namespaceLoading,
    namespaceLoadFailed,
    chunkSizeOptions,
  ]);

  return (
    <Wizard
      steps={steps}
      activeStepIndex={activeStepIndex}
      onNavigate={({ detail }) => {
        const requested = detail.requestedStepIndex;
        if (requested > activeStepIndex) {
          if (activeStepIndex === 1) {
            const errs = validateStep2(model, docTab, t, resourcePrefix);
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
        // App-specific copy; everything else comes from the I18nProvider.
        submitButton: t("Connect source"),
      }}
    />
  );
};
