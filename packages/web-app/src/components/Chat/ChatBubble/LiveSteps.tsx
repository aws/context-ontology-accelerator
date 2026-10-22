// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { StatusIndicator } from "@cloudscape-design/components";
import type { TraceStep } from "@app-types";
import { localizeLiveStep } from "@components/Chat/step-labels";
import { useAppI18n } from "../../../i18n/LocaleProvider";

export interface LiveStepsProps {
  steps: TraceStep[] | undefined;
}

export const LiveSteps: React.FC<LiveStepsProps> = ({ steps }) => {
  const { t } = useAppI18n();
  const traceSteps = steps ?? [];
  const currentStep = traceSteps[traceSteps.length - 1];
  const label = currentStep
    ? t("Resolving query · {step}", {
        step: localizeLiveStep(t, currentStep.step),
      })
    : t("Resolving query");

  return <StatusIndicator type="loading">{label}</StatusIndicator>;
};
