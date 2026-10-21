// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { ontologyTypeGroup } from "@utils/ontology-display";
import { useAppI18n } from "../../i18n/LocaleProvider";

/**
 * The warning shown inside every ontology delete-confirm modal.
 *
 * Single home for both halves of the copy. The header used to come from
 * `deleteOntologyCopy` while each page hand-wrote its own body, so the two
 * descriptions of the same destructive action could drift apart.
 */
export function OntologyDeleteWarning({
  ontologyType,
  name,
}: {
  ontologyType: string;
  /** Display name of the ontology, emphasised inline in the first sentence. */
  name: string;
}) {
  const { t } = useAppI18n();
  const group = ontologyTypeGroup(ontologyType);
  const header =
    group === "foundational"
      ? t("Remove this foundational ontology?")
      : t("This action cannot be undone");

  return (
    <Alert type="warning" header={header}>
      {group === "foundational" ? (
        <>
          {t("Removing ")}
          <b>{name}</b>
          {t(
            " deletes its graph triples and vector embeddings from this namespace, so it will no longer be available for grounding. It's a curated reference, so you can re-load it from the catalog afterward if needed.",
          )}
        </>
      ) : group === "induced" ? (
        <SpaceBetween size="s">
          <Box variant="span">
            {t("Deleting ")}
            <b>{name}</b>
            {t(
              " permanently removes its classes, properties, graph triples, and vector embeddings from this namespace, so it can no longer ground future inductions and queries using its classes will stop returning results.",
            )}
          </Box>
          <Box variant="span">
            {t(
              "It also deletes the proposals tied to it — both those that produced it and any grounded against it. This is generated content and cannot be recovered; re-creating it means re-running induction.",
            )}
          </Box>
        </SpaceBetween>
      ) : (
        <>
          {t("Deleting ")}
          <b>{name}</b>
          {t(
            " permanently removes its graph triples, vector embeddings, and registry entry from this namespace. This is your own content and cannot be recovered.",
          )}
        </>
      )}
    </Alert>
  );
}
