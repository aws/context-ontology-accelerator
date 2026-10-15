// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type {
  CloudFormationCustomResourceEvent,
  CloudFormationCustomResourceResponse,
} from "aws-lambda";
import {
  DataZoneClient,
  CreateAssetTypeCommand,
  ConflictException,
  DeleteAssetTypeCommand,
  GetAssetTypeCommand,
  ResourceNotFoundException,
} from "@aws-sdk/client-datazone";

const dz = new DataZoneClient({});

export const handler = async (
  event: CloudFormationCustomResourceEvent,
): Promise<CloudFormationCustomResourceResponse> => {
  console.log("Event:", JSON.stringify(event, null, 2));

  const props = event.ResourceProperties;
  const domainId = props.DomainId;
  const projectId = props.ProjectId;
  const name = props.Name;
  const description = props.Description;
  const formName = props.FormName;
  const formRevision = props.FormRevision;

  try {
    switch (event.RequestType) {
      case "Create":
        return await handleCreateOrUpdate(event, {
          domainId,
          projectId,
          name,
          description,
          formName,
          formRevision,
        });
      case "Update":
        return await handleUpdate(event, {
          domainId,
          projectId,
          name,
          description,
          formName,
          formRevision,
        });
      case "Delete":
        return await handleDelete(event, { domainId, projectId, name });
      default:
        throw new Error(
          `Unsupported CloudFormation request type: ${(event as any).RequestType}`,
        );
    }
  } catch (error) {
    console.error("Error:", error);
    throw new Error(error instanceof Error ? error.message : String(error));
  }
};

async function handleCreateOrUpdate(
  event: CloudFormationCustomResourceEvent,
  params: {
    domainId: string;
    projectId: string;
    name: string;
    description: string;
    formName: string;
    formRevision: string;
  },
): Promise<CloudFormationCustomResourceResponse> {
  try {
    const resp = await dz.send(
      new CreateAssetTypeCommand({
        domainIdentifier: params.domainId,
        owningProjectIdentifier: params.projectId,
        name: params.name,
        description: params.description,
        formsInput: {
          [params.formName]: {
            typeIdentifier: params.formName,
            typeRevision: params.formRevision,
            required: false,
          },
        },
      }),
    );

    return {
      Status: "SUCCESS",
      PhysicalResourceId: `${params.domainId}/${params.name}`,
      StackId: event.StackId,
      RequestId: event.RequestId,
      LogicalResourceId: event.LogicalResourceId,
      Data: { Revision: resp.revision ?? "1" },
    };
  } catch (error) {
    if (error instanceof ConflictException) {
      return {
        Status: "SUCCESS",
        PhysicalResourceId: `${params.domainId}/${params.name}`,
        StackId: event.StackId,
        RequestId: event.RequestId,
        LogicalResourceId: event.LogicalResourceId,
        Data: { Revision: "existing" },
      };
    }
    throw error;
  }
}

type AssetTypeParams = {
  domainId: string;
  projectId: string;
  name: string;
  description: string;
  formName: string;
  formRevision: string;
};

/**
 * Form-type revision the asset type's latest revision pins for `formName`,
 * or `undefined` when the asset type does not exist.
 */
async function pinnedFormRevision(
  params: AssetTypeParams,
): Promise<{ assetTypeRevision?: string; formRevision?: string } | undefined> {
  try {
    const resp = await dz.send(
      new GetAssetTypeCommand({
        domainIdentifier: params.domainId,
        identifier: params.name,
      }),
    );
    return {
      assetTypeRevision: resp.revision,
      formRevision: resp.formsOutput?.[params.formName]?.typeRevision,
    };
  } catch (error) {
    if (error instanceof ResourceNotFoundException) {
      return undefined;
    }
    throw error;
  }
}

/**
 * Update: make the asset type pin the form-type revision CloudFormation just
 * produced.
 *
 * A changed form model (e.g. a new optional member) yields a new form-type
 * revision, but assets validate against the forms their asset type pins.
 * Create's "ConflictException means it already exists" shortcut would report
 * SUCCESS while the asset type kept pinning the old revision, so Update
 * instead:
 *   1. returns immediately when the asset type already pins the target
 *      revision (idempotent re-runs and unrelated property changes);
 *   2. otherwise calls CreateAssetType with the same name, which records a
 *      new asset-type revision pinning the new form revision;
 *   3. re-reads the asset type and fails the update (so CloudFormation rolls
 *      back) unless the new revision is pinned. It never reports a
 *      propagation that did not happen.
 * The physical resource id is unchanged, so CloudFormation never issues a
 * Delete for the existing asset type.
 */
async function handleUpdate(
  event: CloudFormationCustomResourceEvent,
  params: AssetTypeParams,
): Promise<CloudFormationCustomResourceResponse> {
  const physicalResourceId =
    (event as { PhysicalResourceId?: string }).PhysicalResourceId ??
    `${params.domainId}/${params.name}`;
  const respond = (revision: string | undefined) => ({
    Status: "SUCCESS" as const,
    PhysicalResourceId: physicalResourceId,
    StackId: event.StackId,
    RequestId: event.RequestId,
    LogicalResourceId: event.LogicalResourceId,
    Data: { Revision: revision ?? "unknown" },
  });

  const before = await pinnedFormRevision(params);
  if (before?.formRevision === params.formRevision) {
    return respond(before.assetTypeRevision);
  }

  try {
    await dz.send(
      new CreateAssetTypeCommand({
        domainIdentifier: params.domainId,
        owningProjectIdentifier: params.projectId,
        name: params.name,
        description: params.description,
        formsInput: {
          [params.formName]: {
            typeIdentifier: params.formName,
            typeRevision: params.formRevision,
            required: false,
          },
        },
      }),
    );
  } catch (error) {
    if (error instanceof ConflictException) {
      throw new Error(
        `Asset type ${params.name} pins ${params.formName} revision ` +
          `${before?.formRevision ?? "(none)"}, and DataZone refused a new ` +
          `revision pinning ${params.formRevision}: ${error.message}`,
      );
    }
    throw error;
  }

  // GetAssetType can briefly lag the create; retry a few times before failing.
  let after = await pinnedFormRevision(params);
  for (let attempt = 0; attempt < 3 && after?.formRevision !== params.formRevision; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 2000 * (attempt + 1)));
    after = await pinnedFormRevision(params);
  }
  if (after?.formRevision !== params.formRevision) {
    throw new Error(
      `Asset type ${params.name} still pins ${params.formName} revision ` +
        `${after?.formRevision ?? "(none)"} after update; expected ${params.formRevision}`,
    );
  }
  return respond(after.assetTypeRevision);
}

async function handleDelete(
  event: CloudFormationCustomResourceEvent,
  params: { domainId: string; projectId: string; name: string },
): Promise<CloudFormationCustomResourceResponse> {
  try {
    await dz.send(
      new DeleteAssetTypeCommand({
        domainIdentifier: params.domainId,
        identifier: params.name,
      }),
    );
  } catch (error) {
    // Ignore not-found on delete — resource may already be gone
    console.log("Delete asset type (best-effort):", error);
  }

  return {
    Status: "SUCCESS",
    PhysicalResourceId:
      (event as any).PhysicalResourceId ?? `${params.domainId}/${params.name}`,
    StackId: event.StackId,
    RequestId: event.RequestId,
    LogicalResourceId: event.LogicalResourceId,
  };
}
