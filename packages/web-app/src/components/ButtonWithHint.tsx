// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useRef, useState } from "react";
import type { ReactNode } from "react";
import Button from "@cloudscape-design/components/button";
import type { ButtonProps } from "@cloudscape-design/components/button";
import Tooltip from "@cloudscape-design/components/tooltip";

export type ButtonWithHintProps = ButtonProps & { hint: ReactNode };

/**
 * A Button that reveals an explanatory hint on hover/focus, via a Cloudscape
 * Tooltip anchored to the button. Keeps the explanation off the button's own
 * accessible name and avoids a separate info icon next to actionable controls.
 */
export function ButtonWithHint({ hint, ...buttonProps }: ButtonWithHintProps) {
  const [showHint, setShowHint] = useState(false);
  const wrapperRef = useRef<HTMLSpanElement>(null);
  return (
    <span
      ref={wrapperRef}
      onMouseEnter={() => setShowHint(true)}
      onMouseLeave={() => setShowHint(false)}
      onFocus={() => setShowHint(true)}
      onBlur={() => setShowHint(false)}
    >
      <Button {...buttonProps} />
      {showHint && (
        <Tooltip
          content={hint}
          position="bottom"
          getTrack={() => wrapperRef.current}
        />
      )}
    </span>
  );
}
