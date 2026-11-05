// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type {
  CloudFormationCustomResourceEvent,
  CloudFormationCustomResourceCreateEvent,
  CloudFormationCustomResourceUpdateEvent,
  CloudFormationCustomResourceDeleteEvent,
  CloudFormationCustomResourceResponse,
} from "aws-lambda";
import {
  AdminAddUserToGroupCommand,
  AdminCreateUserCommand,
  AdminDeleteUserCommand,
  CognitoIdentityProviderClient,
  CreateGroupCommand,
  DeleteGroupCommand,
  ResourceNotFoundException,
  UserNotFoundException,
} from "@aws-sdk/client-cognito-identity-provider";
import { DEFAULT_ADMIN_EMAIL } from "../constants";

const cognito = new CognitoIdentityProviderClient({});

/**
 * Validate required custom resource properties.
 * Email is passed directly — no Secrets Manager involved.
 */
function validateProps(props: Record<string, unknown>): {
  userPoolId: string;
  email: string;
  username: string;
  group: string;
} {
  const userPoolId = props.UserPoolId;
  const email = props.Email;
  const username = props.Username;
  const group = props.Group;
  if (!userPoolId || typeof userPoolId !== "string")
    throw new Error("Missing or invalid UserPoolId in ResourceProperties");
  if (!email || typeof email !== "string")
    throw new Error("Missing or invalid Email in ResourceProperties");
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))
    throw new Error("Email must be a valid email address format");
  if (!username || typeof username !== "string")
    throw new Error("Missing or invalid Username in ResourceProperties");
  if (!group || typeof group !== "string")
    throw new Error("Missing or invalid Group in ResourceProperties");
  return { userPoolId, email, username, group };
}

export const handler = async (
  event: CloudFormationCustomResourceEvent,
): Promise<CloudFormationCustomResourceResponse> => {
  const sanitizedEvent = {
    ...event,
    ResourceProperties: {
      ...event.ResourceProperties,
      Username: "[REDACTED]",
      Email: "[REDACTED]",
    },
  };
  console.log("Event:", JSON.stringify(sanitizedEvent, null, 2));

  try {
    switch (event.RequestType) {
      case "Create":
        return await handleCreate(event);
      case "Update":
        return await handleUpdate(event);
      case "Delete":
        return await handleDelete(event);
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

async function handleCreate(
  event: CloudFormationCustomResourceCreateEvent,
): Promise<CloudFormationCustomResourceResponse> {
  const { userPoolId, email, username, group } = validateProps(
    event.ResourceProperties,
  );
  await createUserAndGroup(userPoolId, username, email, group);
  return {
    Status: "SUCCESS",
    PhysicalResourceId: `${userPoolId}/${username}`,
    StackId: event.StackId,
    RequestId: event.RequestId,
    LogicalResourceId: event.LogicalResourceId,
  };
}

async function handleUpdate(
  event: CloudFormationCustomResourceUpdateEvent,
): Promise<CloudFormationCustomResourceResponse> {
  const { userPoolId, email, username, group } = validateProps(
    event.ResourceProperties,
  );
  await createUserAndGroup(userPoolId, username, email, group);
  return {
    Status: "SUCCESS",
    PhysicalResourceId: `${userPoolId}/${username}`,
    StackId: event.StackId,
    RequestId: event.RequestId,
    LogicalResourceId: event.LogicalResourceId,
  };
}

async function handleDelete(
  event: CloudFormationCustomResourceDeleteEvent,
): Promise<CloudFormationCustomResourceResponse> {
  const { userPoolId, username, group } = validateProps(
    event.ResourceProperties,
  );
  await deleteUserAndGroup(userPoolId, username, group);
  return {
    Status: "SUCCESS",
    PhysicalResourceId: event.PhysicalResourceId,
    StackId: event.StackId,
    RequestId: event.RequestId,
    LogicalResourceId: event.LogicalResourceId,
  };
}

/**
 * Create the admin user with a temporary password (Cognito emails it, except to
 * the placeholder address) and assign them to the admin group.
 *
 * No permanent password is set — the user must change it on first login.
 * No Secrets Manager secret is created.
 */
async function createUserAndGroup(
  userPoolId: string,
  username: string,
  email: string,
  group: string,
): Promise<void> {
  try {
    const res = await cognito.send(
      new AdminCreateUserCommand({
        UserPoolId: userPoolId,
        Username: username,
        UserAttributes: [
          { Name: "email", Value: email },
          { Name: "email_verified", Value: "true" },
        ],
        // The placeholder address is never sent a temporary password.
        ...(email === DEFAULT_ADMIN_EMAIL
          ? { MessageAction: "SUPPRESS" as const }
          : { DesiredDeliveryMediums: ["EMAIL" as const] }),
      }),
    );
    if (!res.User) throw new Error("Failed to create user");
  } catch (e: unknown) {
    if (
      e &&
      typeof e === "object" &&
      "name" in e &&
      e.name !== "UsernameExistsException"
    )
      throw e;
  }

  try {
    await cognito.send(
      new CreateGroupCommand({
        UserPoolId: userPoolId,
        GroupName: group,
        Description: "Initial admin group",
      }),
    );
  } catch (e: unknown) {
    if (
      e &&
      typeof e === "object" &&
      "name" in e &&
      e.name !== "GroupExistsException"
    )
      throw e;
  }

  await cognito.send(
    new AdminAddUserToGroupCommand({
      UserPoolId: userPoolId,
      Username: username,
      GroupName: group,
    }),
  );
}

async function deleteUserAndGroup(
  userPoolId: string,
  username: string,
  group: string,
): Promise<void> {
  try {
    await cognito.send(
      new AdminDeleteUserCommand({
        UserPoolId: userPoolId,
        Username: username,
      }),
    );
  } catch (e) {
    if (
      !(e instanceof UserNotFoundException) &&
      !(e instanceof ResourceNotFoundException)
    )
      throw e;
  }

  try {
    await cognito.send(
      new DeleteGroupCommand({ UserPoolId: userPoolId, GroupName: group }),
    );
  } catch (e) {
    if (!(e instanceof ResourceNotFoundException)) throw e;
  }
}
