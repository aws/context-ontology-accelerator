// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { Box, Button, SpaceBetween } from "@cloudscape-design/components";
import type { ExampleQuestion } from "@api-hooks/use-example-questions";
import { useAppI18n } from "../../../i18n/LocaleProvider";

export interface SamplePromptsProps {
  questions: ExampleQuestion[];
  onSelect: (question: string) => void;
  disabled?: boolean;
}

/**
 * Empty-state sample prompts for the playground. Renders one clickable chip per
 * example question; clicking sends the full question to the serve layer. Shown
 * only when the chat is empty and questions were published for the namespace.
 */
export const SamplePrompts: React.FC<SamplePromptsProps> = ({
  questions,
  onSelect,
  disabled = false,
}) => {
  const { t } = useAppI18n();

  if (questions.length === 0) {
    return (
      <Box textAlign="center" color="text-body-secondary" padding="xl">
        {t("Ask a question against the governed surface...")}
      </Box>
    );
  }

  return (
    <Box textAlign="center" padding="xl">
      <SpaceBetween size="m" alignItems="center">
        <Box color="text-body-secondary">
          {t("Try one of the sample prompts below, or type your own question.")}
        </Box>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "8px",
            justifyContent: "center",
            maxWidth: "760px",
          }}
        >
          {questions.map((q, i) => (
            <Button
              key={`${q.question}-${i}`}
              variant="normal"
              disabled={disabled}
              onClick={() => onSelect(q.question)}
              ariaLabel={q.question}
            >
              {q.label ?? q.question}
            </Button>
          ))}
        </div>
      </SpaceBetween>
    </Box>
  );
};
