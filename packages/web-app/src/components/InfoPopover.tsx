// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Popover from "@cloudscape-design/components/popover";
import Icon from "@cloudscape-design/components/icon";
import type { ReactNode } from "react";
import { useAppI18n } from "../i18n/LocaleProvider";

export interface InfoPopoverProps {
  /** Optional bold header shown at the top of the popover. */
  readonly header?: string;
  /** Popover body: pass `<Box variant="p">` / `<SpaceBetween>` content. */
  readonly children: ReactNode;
  /** Accessible label for the trigger icon (falls back to the header). */
  readonly ariaLabel?: string;
}

/**
 * A small circled-"i" information affordance: a `status-info` icon that opens a
 * Popover explaining a concept in plain language. Generalizes the inline
 * `AiEnrichedHint` pattern (see `TableDetail.tsx`) so every surface uses one
 * consistent explain-this control.
 *
 * Drop it into a Cloudscape `Header` / `FormField` `info` slot or an
 * `ExpandableSection` `headerInfo` slot; or render it as a sibling next to a
 * label / button / column header (wrap both in a horizontal `SpaceBetween`) for
 * hosts that have no info slot (KeyValuePairs, Button, table headers).
 */
export function InfoPopover({ header, children, ariaLabel }: InfoPopoverProps) {
  const { t } = useAppI18n();

  return (
    <Popover
      triggerType="custom"
      size="large"
      dismissButton={false}
      header={header}
      content={children}
    >
      <Icon
        name="status-info"
        variant="subtle"
        ariaLabel={ariaLabel ?? header ?? t("More info")}
      />
    </Popover>
  );
}
