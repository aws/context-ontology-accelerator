// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Textarea from "@cloudscape-design/components/textarea";
import Button from "@cloudscape-design/components/button";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Modal from "@cloudscape-design/components/modal";
import RadioGroup from "@cloudscape-design/components/radio-group";
import { useCreateNamespace } from "@api-hooks";
import { useUser } from "@auth";
import {
  extractErrorMessage,
  formatNamespaceLanguage,
  isNamespaceLanguage,
  NAMESPACE_LANGUAGE_OPTIONS,
} from "@utils";
import type { NamespaceLanguage } from "@utils";
import { AccessDeniedError } from "@coa/control-plane-client";

/** Convert a display name to a machine-friendly identifier. */
const toMachineName = (value: string): string =>
  value
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9-]/g, "")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");

export const CreateNamespace: React.FC = () => {
  const navigate = useNavigate();
  const { user } = useUser();
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [language, setLanguage] = useState<NamespaceLanguage>("en");
  const [nameError, setNameError] = useState("");
  const [showConfirm, setShowConfirm] = useState(false);

  const { mutate, isPending, error } = useCreateNamespace({
    onSuccess: () => navigate("/namespaces"),
    // Close the confirmation so the form-level error alert is visible.
    onError: () => setShowConfirm(false),
  });

  const machineName = toMachineName(displayName);

  const validate = (): boolean => {
    if (!displayName.trim()) {
      setNameError("Namespace name is required.");
      return false;
    }
    if (!machineName || !/^[a-z][a-z0-9-]*$/.test(machineName)) {
      setNameError(
        "Name must start with a lowercase letter and contain only lowercase letters, numbers, and hyphens.",
      );
      return false;
    }
    setNameError("");
    return true;
  };

  // The language is immutable once the namespace exists, so creation always
  // goes through a confirmation step that restates the choice.
  const handleSubmit = () => {
    if (!validate()) return;
    setShowConfirm(true);
  };

  const handleConfirm = () => {
    mutate({
      name: machineName,
      displayName: displayName.trim(),
      owner: user?.email ?? "",
      description: description || undefined,
      language,
    });
  };

  return (
    <Form
      actions={
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={() => navigate("/namespaces")}>
            Cancel
          </Button>
          <Button variant="primary" loading={isPending} onClick={handleSubmit}>
            Create namespace
          </Button>
        </SpaceBetween>
      }
      header={
        <Header
          variant="h1"
          description="Create a new namespace to partition your semantic graph into an isolated scope."
        >
          Create namespace
        </Header>
      }
      errorText={error ? extractErrorMessage(error) : undefined}
    >
      {error && (
        <Alert
          type="error"
          header={
            error instanceof AccessDeniedError
              ? "Unauthorized"
              : "Error creating namespace"
          }
        >
          {extractErrorMessage(error)}
        </Alert>
      )}
      <Container header={<Header variant="h2">Namespace details</Header>}>
        <SpaceBetween size="l">
          <FormField
            label="Namespace name"
            description={`Machine-friendly identifier: ${machineName || "—"}`}
            errorText={nameError}
          >
            <Input
              value={displayName}
              onChange={({ detail }) => {
                setDisplayName(detail.value);
                if (nameError) setNameError("");
              }}
              placeholder="e.g., Insurance Prod"
            />
          </FormField>
          <FormField
            label={
              <>
                Description <i>- optional</i>
              </>
            }
            description="A brief description of the namespace purpose."
          >
            <Textarea
              value={description}
              onChange={({ detail }) => setDescription(detail.value)}
              placeholder="e.g., Production namespace for insurance domain data"
            />
          </FormField>
          <FormField
            label="Language"
            description="Text COA generates (descriptions, synonyms, and labels for document sources) uses this language. Source text and table and column names are kept as-is."
            constraintText="The language cannot be changed after the namespace is created, and it must match the language of the sources you connect. To use a different language, create a new namespace."
          >
            <RadioGroup
              value={language}
              onChange={({ detail }) => {
                if (isNamespaceLanguage(detail.value)) {
                  setLanguage(detail.value);
                }
              }}
              items={NAMESPACE_LANGUAGE_OPTIONS.map((option) => ({
                value: option.value,
                label: option.label,
              }))}
            />
          </FormField>
        </SpaceBetween>
      </Container>
      {showConfirm && (
        <Modal
          visible={showConfirm}
          onDismiss={() => {
            // Keep the confirmation open while the create request is in flight.
            if (!isPending) setShowConfirm(false);
          }}
          header="Confirm namespace language"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  disabled={isPending}
                  onClick={() => setShowConfirm(false)}
                >
                  Back
                </Button>
                <Button
                  variant="primary"
                  loading={isPending}
                  onClick={handleConfirm}
                >
                  Confirm and create
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <KeyValuePairs
              columns={2}
              items={[
                { label: "Namespace name", value: machineName },
                { label: "Language", value: formatNamespaceLanguage(language) },
              ]}
            />
            <Alert type="warning" header="The language cannot be changed">
              This namespace will be created as{" "}
              <b>{formatNamespaceLanguage(language)}</b>. The language cannot be
              changed later, and it must match the language of the sources you
              connect. To use a different language, you must create a new
              namespace.
            </Alert>
          </SpaceBetween>
        </Modal>
      )}
    </Form>
  );
};
