// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import Avatar from "@cloudscape-design/chat-components/avatar";
import { useAppI18n } from "../../../i18n/LocaleProvider";

export interface ChatAvatarProps {
  role: "user" | "assistant";
  initials?: string;
  isLoading?: boolean;
}

export const ChatAvatar: React.FC<ChatAvatarProps> = ({
  role,
  initials,
  isLoading = false,
}) => {
  const { t } = useAppI18n();

  if (role === "assistant") {
    return (
      <Avatar
        color="gen-ai"
        iconName="gen-ai"
        ariaLabel={t("Context Ontology Accelerator")}
        loading={isLoading}
      />
    );
  }

  return (
    <Avatar color="default" initials={initials ?? "U"} ariaLabel={t("User")} />
  );
};
