// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useContext } from "react";
import { RuntimeConfigContext } from "../RuntimeContext";

/**
 * Fixed badge in the bottom-left corner showing the deployed monorepo version
 * (e.g. `v0.2.2`). The value comes from `runtime-config.json` (`version`),
 * which CDK writes from the repo-root VERSION file at deploy time — so the badge
 * always reflects what is actually deployed, with no rebuild.
 *
 * Renders nothing when the version is absent or blank, so it never shows a
 * broken `v`.
 * `pointer-events: none` keeps it from intercepting clicks on the nav beneath it.
 */
export const VersionBadge: React.FC = () => {
  const runtime = useContext(RuntimeConfigContext);
  const version = runtime?.version?.trim();
  if (!version) return null;

  return (
    <div
      aria-label={`Application version ${version}`}
      style={{
        position: "fixed",
        bottom: "0.5rem",
        left: "0.5rem",
        zIndex: 1000,
        pointerEvents: "none",
        fontSize: "0.75rem",
        fontFamily: "monospace",
        opacity: 0.6,
        userSelect: "text",
      }}
    >
      v{version}
    </div>
  );
};
