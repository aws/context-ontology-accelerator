// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * ColumnDenylistEditor — structured editor for a per-table column denylist.
 *
 * Replaces the free-form `table: col1, col2` textarea with a Cloudscape
 * `AttributeEditor`: one row per table, where the table name is a plain text
 * field and the denied columns are edited with {@link TokenInput} chips.
 *
 * The editor operates on an array of {@link ColumnDenylistRow} so that empty
 * or in-progress rows are preserved while editing. Convert to/from the wire
 * shape (`Record<string, string[]>`) with the helpers in
 * `@utils/grant-overrides`.
 */
import React from "react";
import AttributeEditor from "@cloudscape-design/components/attribute-editor";
import Input from "@cloudscape-design/components/input";
import { TokenInput } from "@components/TokenInput";
import { useAppI18n } from "../../i18n/LocaleProvider";

export interface ColumnDenylistRow {
  table: string;
  columns: string[];
}

export interface ColumnDenylistEditorProps {
  value: ColumnDenylistRow[];
  onChange: (rows: ColumnDenylistRow[]) => void;
  disabled?: boolean;
}

export const ColumnDenylistEditor: React.FC<ColumnDenylistEditorProps> = ({
  value,
  onChange,
  disabled = false,
}) => {
  const { t } = useAppI18n();
  const updateRow = (index: number, patch: Partial<ColumnDenylistRow>) => {
    onChange(value.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  };

  return (
    <AttributeEditor<ColumnDenylistRow>
      items={value}
      addButtonText={t("Add table")}
      removeButtonText={t("Remove")}
      empty={t("No column restrictions. Add a table to deny specific columns.")}
      disableAddButton={disabled}
      onAddButtonClick={() => onChange([...value, { table: "", columns: [] }])}
      onRemoveButtonClick={({ detail }) =>
        onChange(value.filter((_, i) => i !== detail.itemIndex))
      }
      definition={[
        {
          label: t("Table"),
          control: (item, index) => (
            <Input
              value={item.table}
              onChange={({ detail }) =>
                updateRow(index, { table: detail.value })
              }
              placeholder="customers"
              ariaLabel={t("Table name for row {row}", { row: index + 1 })}
              disabled={disabled}
            />
          ),
        },
        {
          label: t("Denied columns"),
          control: (item, index) => (
            <TokenInput
              value={item.columns}
              onChange={(columns) => updateRow(index, { columns })}
              placeholder="ssn"
              ariaLabel={t("Denied columns for row {row}", { row: index + 1 })}
              disabled={disabled}
            />
          ),
        },
      ]}
    />
  );
};
