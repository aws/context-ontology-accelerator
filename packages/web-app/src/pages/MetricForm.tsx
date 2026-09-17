// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Textarea from "@cloudscape-design/components/textarea";
import Select from "@cloudscape-design/components/select";
import Autosuggest from "@cloudscape-design/components/autosuggest";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Alert from "@cloudscape-design/components/alert";
import AttributeEditor from "@cloudscape-design/components/attribute-editor";
import Grid from "@cloudscape-design/components/grid";
import {
  useCreateMetric,
  useGetMetric,
  useUpdateMetric,
} from "../api-hooks/use-metrics";
import type {
  CreateMetricInput,
  MetricDefinition,
  MetricDialect,
} from "../api-hooks/use-metrics";
import type { TimeGrain, SqlDialect } from "@coa/control-plane-client";
import { SqlDialect as SqlDialectEnum } from "@coa/control-plane-client";
import { useListSources } from "../api-hooks/use-list-sources";
import { useListSourceTables } from "../api-hooks/use-list-source-tables";
import { useApiClient } from "@components/ApiClientProvider";
import { searchEntities } from "../services/ontology-engine";

const DIALECT_OPTIONS: Array<{ value: SqlDialect; label: string }> = [
  { value: SqlDialectEnum.POSTGRESQL, label: "PostgreSQL" },
  { value: SqlDialectEnum.TRINO, label: "Trino" },
  { value: SqlDialectEnum.REDSHIFT, label: "Redshift" },
  { value: SqlDialectEnum.SNOWFLAKE, label: "Snowflake" },
  { value: SqlDialectEnum.DATABRICKS, label: "Databricks" },
  { value: SqlDialectEnum.MYSQL, label: "MySQL" },
];

// Legacy/imported metrics may store a mis-cased dialect (e.g. "redshift" from an
// OSI import before the parser fix — see #140). The Select matches option values
// case-sensitively, so normalize a loaded dialect to upper-case when it resolves
// to a known option; otherwise leave it untouched so an unknown value is not
// silently coerced to a valid one.
const _DIALECT_VALUES: string[] = DIALECT_OPTIONS.map((o) => o.value);
export const normalizeLoadedDialect = (raw: string): string => {
  if (!raw) return "";
  const upper = raw.toUpperCase();
  return _DIALECT_VALUES.includes(upper) ? upper : raw;
};

const TIME_GRAIN_OPTIONS = [
  { value: "", label: "None" },
  { value: "DAY", label: "Day" },
  { value: "WEEK", label: "Week" },
  { value: "MONTH", label: "Month" },
  { value: "QUARTER", label: "Quarter" },
  { value: "YEAR", label: "Year" },
];

interface DialectRow {
  dialect: string;
  expression: string;
}

export const MetricForm: React.FC = () => {
  const navigate = useNavigate();
  const { namespaceId, metricName } = useParams<{
    namespaceId: string;
    metricName: string;
  }>();

  const isEdit = !!metricName && metricName !== "create";

  const { data: existingData } = useGetMetric(
    namespaceId ?? "",
    isEdit ? metricName : "",
  );

  const createMutation = useCreateMetric(namespaceId ?? "");
  const updateMutation = useUpdateMetric(namespaceId ?? "", metricName ?? "");

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [dataSourceId, setDataSourceId] = useState("");
  const [sourceTable, setSourceTable] = useState("");
  const [defaultTimeGrain, setDefaultTimeGrain] = useState("");
  const [unit, setUnit] = useState("");
  const [returnType, setReturnType] = useState("");

  // Fetch sources for the data source dropdown
  const { data: sourcesData } = useListSources(
    namespaceId ?? "",
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    "DATABASE" as any,
  );
  const sourceOptions = useMemo(() => {
    const items = sourcesData?.pages.flatMap((p) => p.items ?? []) ?? [];
    return items
      .filter((s) => s.status === "APPROVED" || s.status === "COMPLETED")
      .map((s) => ({
        value: s.sourceId ?? "",
        label: s.name ?? s.sourceId ?? "",
      }));
  }, [sourcesData]);

  // Fetch tables for the selected source
  const { data: tablesData, isFetching: tablesFetching } = useListSourceTables(
    namespaceId ?? "",
    dataSourceId,
  );
  const tableOptions = useMemo(() => {
    const items = tablesData?.items ?? [];
    return items.map((t) => ({
      value: t.name ?? t.tableId ?? "",
      label: t.name ?? t.tableId ?? "",
    }));
  }, [tablesData]);

  const [dialects, setDialects] = useState<DialectRow[]>([
    { dialect: SqlDialectEnum.POSTGRESQL, expression: "" },
  ]);
  const [synonyms, setSynonyms] = useState<string[]>([]);
  const [synonymInput, setSynonymInput] = useState("");
  const [instructions, setInstructions] = useState("");
  const [examples, setExamples] = useState<Array<{ value: string }>>([]);
  const [ontologyConcepts, setOntologyConcepts] = useState<string[]>([]);
  const [conceptInput, setConceptInput] = useState("");
  const [conceptOptions, setConceptOptions] = useState<
    Array<{ value: string; label: string; description?: string }>
  >([]);
  const [conceptLoading, setConceptLoading] = useState(false);
  const [conceptError, setConceptError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);

  const apiClient = useApiClient();

  const handleConceptSearch = useCallback(
    async (text: string) => {
      if (!text.trim() || !namespaceId) {
        setConceptOptions([]);
        return;
      }
      setConceptLoading(true);
      setConceptError(null);
      try {
        const hits = await searchEntities(apiClient, namespaceId, text, {
          kind: "class",
          exclude_ontology_id: "urn:coa:vocab#GovernedMetrics",
          limit: 15,
        });
        setConceptOptions(
          hits.map((h) => ({
            value: h.uri,
            label: h.label,
            description: h.uri,
          })),
        );
      } catch (err) {
        setConceptOptions([]);
        setConceptError(
          err instanceof Error
            ? err.message
            : "Failed to search ontology classes",
        );
      } finally {
        setConceptLoading(false);
      }
    },
    [apiClient, namespaceId],
  );

  useEffect(() => {
    if (isEdit && existingData?.metric) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const m = existingData.metric as Record<string, any>;
      setName(m.name ?? "");
      setDescription(m.description ?? "");
      setDataSourceId(m.dataSourceId ?? "");
      setSourceTable(m.sourceTable ?? "");
      setDefaultTimeGrain(m.defaultTimeGrain ?? "");
      setUnit(m.unit ?? "");
      setReturnType(m.returnType ?? "");
      setDialects(
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (m.expression?.dialects ?? []).map((d: any) => ({
          dialect: normalizeLoadedDialect(d.dialect ?? ""),
          expression: d.expression ?? "",
        })),
      );
      if (m.aiContext?.synonyms) {
        setSynonyms(m.aiContext.synonyms);
      }
      if (m.aiContext?.instructions) {
        setInstructions(m.aiContext.instructions);
      }
      if (m.aiContext?.examples) {
        setExamples(m.aiContext.examples.map((e: string) => ({ value: e })));
      }
      if ((existingData.metric as MetricDefinition).ontologyConcepts) {
        setOntologyConcepts(
          (existingData.metric as MetricDefinition).ontologyConcepts ?? [],
        );
      }
    }
  }, [isEdit, existingData]);

  const handleSubmit = () => {
    setFormError(null);
    setWarnings([]);

    if (!name.trim()) {
      setFormError("Name is required");
      return;
    }
    if (!description.trim()) {
      setFormError("Description is required");
      return;
    }
    if (!dataSourceId.trim()) {
      setFormError("Data source is required");
      return;
    }
    if (!sourceTable.trim()) {
      setFormError("Source table is required");
      return;
    }
    const validDialects = dialects.filter(
      (d) => d.dialect && d.expression.trim(),
    );
    if (validDialects.length === 0) {
      setFormError("At least one dialect expression is required");
      return;
    }

    const input: CreateMetricInput = {
      name: name.trim(),
      description: description.trim(),
      dataSourceId: dataSourceId.trim(),
      sourceTable: sourceTable.trim(),
      expression: {
        dialects: validDialects as MetricDialect[],
      },
      ...(defaultTimeGrain && {
        defaultTimeGrain: defaultTimeGrain as TimeGrain,
      }),
      ...(unit.trim() && { unit: unit.trim() }),
      ...(returnType.trim() && { returnType: returnType.trim() }),
    };

    const exList = examples.map((e) => e.value).filter(Boolean);
    if (synonyms.length > 0 || instructions.trim() || exList.length > 0) {
      input.aiContext = {
        ...(synonyms.length > 0 && { synonyms }),
        ...(instructions.trim() && { instructions: instructions.trim() }),
        ...(exList.length > 0 && { examples: exList }),
      };
    }

    if (ontologyConcepts.length > 0) {
      (input as Record<string, unknown>).ontologyConcepts = ontologyConcepts;
    }

    const mutation = isEdit ? updateMutation : createMutation;
    mutation.mutate(input, {
      onSuccess: (result) => {
        if (result.warnings && result.warnings.length > 0) {
          setWarnings(result.warnings.map((w) => `${w.field}: ${w.message}`));
        }
        navigate(`/namespaces/${namespaceId}/metrics/${result.metric.name}`);
      },
      onError: (err) => {
        setFormError(err.message);
      },
    });
  };

  const isPending = createMutation.isPending || updateMutation.isPending;

  return (
    <Form
      header={
        <Header variant="h1">
          {isEdit ? `Edit ${metricName}` : "Create metric"}
        </Header>
      }
      actions={
        <SpaceBetween direction="horizontal" size="xs">
          <Button
            variant="link"
            onClick={() => navigate(`/namespaces/${namespaceId}/metrics`)}
          >
            Cancel
          </Button>
          <Button variant="primary" onClick={handleSubmit} loading={isPending}>
            {isEdit ? "Save changes" : "Create metric"}
          </Button>
        </SpaceBetween>
      }
      errorText={formError}
    >
      <SpaceBetween size="l">
        {warnings.length > 0 && (
          <Alert type="warning" dismissible onDismiss={() => setWarnings([])}>
            {warnings.join("; ")}
          </Alert>
        )}

        <Container header={<Header variant="h2">Basic information</Header>}>
          <SpaceBetween size="m">
            <FormField
              label="Name"
              constraintText="Alphanumeric and underscores, 1-128 characters"
            >
              <Input
                value={name}
                onChange={({ detail }) => setName(detail.value)}
                disabled={isEdit}
                placeholder="monthly_revenue"
              />
            </FormField>
            <FormField label="Description">
              <Textarea
                value={description}
                onChange={({ detail }) => setDescription(detail.value)}
                placeholder="Total revenue from all orders in a calendar month"
              />
            </FormField>
          </SpaceBetween>
        </Container>

        <Container header={<Header variant="h2">Source</Header>}>
          <SpaceBetween size="m">
            <FormField label="Data source">
              <Select
                selectedOption={
                  sourceOptions.find((o) => o.value === dataSourceId) ?? null
                }
                onChange={({ detail }) => {
                  const newId = detail.selectedOption.value ?? "";
                  setDataSourceId(newId);
                  if (newId !== dataSourceId) setSourceTable("");
                }}
                options={sourceOptions}
                placeholder="Select a data source"
                filteringType="auto"
                loadingText="Loading sources"
                empty="No sources available"
              />
            </FormField>
            <FormField label="Source table">
              <Select
                selectedOption={
                  tableOptions.find((o) => o.value === sourceTable) ?? null
                }
                onChange={({ detail }) =>
                  setSourceTable(detail.selectedOption.value ?? "")
                }
                options={tableOptions}
                placeholder="Select a table"
                filteringType="auto"
                statusType={tablesFetching ? "loading" : "finished"}
                loadingText="Loading tables"
                empty={
                  dataSourceId
                    ? "No tables found for this source"
                    : "Select a data source first"
                }
                disabled={!dataSourceId}
              />
            </FormField>
            <FormField
              label="Default time grain (optional)"
              description="Default time granularity for aggregation (e.g., DAY, MONTH). Used when no grain is specified in a query."
            >
              <Select
                selectedOption={
                  TIME_GRAIN_OPTIONS.find(
                    (o) => o.value === defaultTimeGrain,
                  ) ?? TIME_GRAIN_OPTIONS[0]
                }
                onChange={({ detail }) =>
                  setDefaultTimeGrain(detail.selectedOption.value ?? "")
                }
                options={TIME_GRAIN_OPTIONS}
              />
            </FormField>
            <FormField
              label="Unit (optional)"
              description="Unit of measurement for the metric result (e.g., USD, claims, percentage)."
            >
              <Input
                value={unit}
                onChange={({ detail }) => setUnit(detail.value)}
                placeholder="currency"
              />
            </FormField>
            <FormField
              label="Return type (optional)"
              description="Data type returned by the expression (e.g., INTEGER, DECIMAL, FLOAT)."
            >
              <Input
                value={returnType}
                onChange={({ detail }) => setReturnType(detail.value)}
                placeholder="xsd:decimal"
              />
            </FormField>
          </SpaceBetween>
        </Container>

        <Container
          header={
            <Header
              variant="h2"
              description="SQL expression to compute this metric. Add one per SQL dialect your sources use."
            >
              Expressions
            </Header>
          }
        >
          <AttributeEditor
            items={dialects}
            onAddButtonClick={() =>
              setDialects([
                ...dialects,
                { dialect: SqlDialectEnum.POSTGRESQL, expression: "" },
              ])
            }
            onRemoveButtonClick={({ detail }) => {
              const updated = [...dialects];
              updated.splice(detail.itemIndex, 1);
              setDialects(updated);
            }}
            addButtonText="Add dialect"
            removeButtonText="Remove"
            definition={[
              {
                label: "Dialect",
                control: (item, itemIndex) => (
                  <Select
                    selectedOption={
                      DIALECT_OPTIONS.find((o) => o.value === item.dialect) ??
                      DIALECT_OPTIONS[0]
                    }
                    onChange={({ detail }) => {
                      const updated = [...dialects];
                      updated[itemIndex] = {
                        ...updated[itemIndex],
                        dialect:
                          detail.selectedOption.value ??
                          SqlDialectEnum.POSTGRESQL,
                      };
                      setDialects(updated);
                    }}
                    options={DIALECT_OPTIONS}
                  />
                ),
              },
              {
                label: "SQL Expression",
                control: (item, itemIndex) => (
                  <Textarea
                    value={item.expression}
                    onChange={({ detail }) => {
                      const updated = [...dialects];
                      updated[itemIndex] = {
                        ...updated[itemIndex],
                        expression: detail.value,
                      };
                      setDialects(updated);
                    }}
                    placeholder="SUM(orders.total_amount)"
                    rows={2}
                  />
                ),
              },
            ]}
          />
        </Container>

        <Container header={<Header variant="h2">AI Context (optional)</Header>}>
          <SpaceBetween size="m">
            <FormField
              label="Synonyms"
              description="Alternative names users might use when asking about this metric."
            >
              <SpaceBetween size="xs">
                <Grid gridDefinition={[{ colspan: 10 }, { colspan: 2 }]}>
                  <Input
                    value={synonymInput}
                    onChange={({ detail }) => setSynonymInput(detail.value)}
                    placeholder="Add synonym and press Enter"
                    onKeyDown={({ detail }) => {
                      if (detail.key === "Enter" && synonymInput.trim()) {
                        setSynonyms([...synonyms, synonymInput.trim()]);
                        setSynonymInput("");
                      }
                    }}
                  />
                  <Button
                    onClick={() => {
                      if (synonymInput.trim()) {
                        setSynonyms([...synonyms, synonymInput.trim()]);
                        setSynonymInput("");
                      }
                    }}
                  >
                    Add
                  </Button>
                </Grid>
                {synonyms.length > 0 && (
                  <SpaceBetween direction="horizontal" size="xs">
                    {synonyms.map((s, i) => (
                      <Button
                        key={i}
                        variant="inline-link"
                        onClick={() =>
                          setSynonyms(synonyms.filter((_, idx) => idx !== i))
                        }
                      >
                        {s} ✕
                      </Button>
                    ))}
                  </SpaceBetween>
                )}
              </SpaceBetween>
            </FormField>
            <FormField
              label="Instructions"
              description="Guidance for the AI on when and how to use this metric."
            >
              <Textarea
                value={instructions}
                onChange={({ detail }) => setInstructions(detail.value)}
                placeholder="Use with order_date as time dimension."
                rows={3}
              />
            </FormField>
            <FormField
              label="Examples"
              description="Example natural language questions this metric answers."
            >
              <AttributeEditor
                items={examples}
                onAddButtonClick={() =>
                  setExamples([...examples, { value: "" }])
                }
                onRemoveButtonClick={({ detail }) => {
                  const updated = [...examples];
                  updated.splice(detail.itemIndex, 1);
                  setExamples(updated);
                }}
                addButtonText="Add example"
                removeButtonText="Remove"
                definition={[
                  {
                    label: "Question",
                    control: (item, itemIndex) => (
                      <Input
                        value={item.value}
                        onChange={({ detail }) => {
                          const updated = [...examples];
                          updated[itemIndex] = { value: detail.value };
                          setExamples(updated);
                        }}
                        placeholder="What was the total revenue last month?"
                      />
                    ),
                  },
                ]}
              />
            </FormField>
          </SpaceBetween>
        </Container>

        <Container
          header={<Header variant="h2">Ontology Links (optional)</Header>}
        >
          <FormField
            label="Ontology classes"
            description="Search for classes from your ontology to link to this metric."
            errorText={conceptError}
          >
            <SpaceBetween size="xs">
              <Autosuggest
                value={conceptInput}
                onChange={({ detail }) => setConceptInput(detail.value)}
                onLoadItems={({ detail }) =>
                  handleConceptSearch(detail.filteringText)
                }
                onSelect={({ detail }) => {
                  const uri = detail.value;
                  if (uri && !ontologyConcepts.includes(uri)) {
                    setOntologyConcepts([...ontologyConcepts, uri]);
                  }
                  setConceptInput("");
                }}
                options={conceptOptions}
                statusType={conceptLoading ? "loading" : "finished"}
                placeholder="Type to search classes…"
                empty="No classes found"
                loadingText="Searching…"
                filteringType="manual"
                enteredTextLabel={(v) => `Use: "${v}"`}
              />
              {ontologyConcepts.length > 0 && (
                <SpaceBetween direction="horizontal" size="xs">
                  {ontologyConcepts.map((c, i) => (
                    <Button
                      key={i}
                      variant="inline-link"
                      onClick={() =>
                        setOntologyConcepts(
                          ontologyConcepts.filter((_, idx) => idx !== i),
                        )
                      }
                    >
                      {c.includes("/") ? c.split("/").pop() : c} ✕
                    </Button>
                  ))}
                </SpaceBetween>
              )}
            </SpaceBetween>
          </FormField>
        </Container>
      </SpaceBetween>
    </Form>
  );
};
