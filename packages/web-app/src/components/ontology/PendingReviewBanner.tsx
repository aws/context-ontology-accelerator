// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Banner shown when there are pending proposals awaiting review.
 * Appears on the Induced Ontology Detail page and the Graph Search page.
 */
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import Alert from "@cloudscape-design/components/alert";
import Button from "@cloudscape-design/components/button";
import {
  listAllProposals,
  type Proposal,
} from "../../services/ontology-engine";
import { useApiClient } from "@components/ApiClientProvider";
import { useAppI18n } from "../../i18n/LocaleProvider";

export function PendingReviewBanner() {
  const { t } = useAppI18n();
  const { namespaceId } = useParams<{ namespaceId: string }>();
  const apiClient = useApiClient();
  const navigate = useNavigate();
  const [pending, setPending] = useState<Proposal[]>([]);

  useEffect(() => {
    if (!namespaceId) return;
    let cancelled = false;
    listAllProposals(apiClient, namespaceId, { status: "pending" })
      .then((items) => {
        if (!cancelled) setPending(items.filter((p) => p.status === "pending"));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [apiClient, namespaceId]);

  if (pending.length === 0) return null;

  return (
    <Alert
      type="info"
      header={t(
        pending.length === 1
          ? "{count} proposal awaiting review"
          : "{count} proposals awaiting review",
        { count: pending.length },
      )}
      action={
        <Button
          onClick={() =>
            navigate(
              `/namespaces/${namespaceId}/ontology/proposals/${pending[0].proposal_id}`,
            )
          }
        >
          {t("Review proposal")}
        </Button>
      }
    >
      {t(
        "New ontology changes have been proposed and are pending your review before they are added to the knowledge graph.",
      )}
    </Alert>
  );
}
