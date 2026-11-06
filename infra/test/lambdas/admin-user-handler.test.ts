// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type {
  CloudFormationCustomResourceCreateEvent,
  CloudFormationCustomResourceUpdateEvent,
  CloudFormationCustomResourceDeleteEvent,
} from "aws-lambda";
import {
  CognitoIdentityProviderClient,
  UserNotFoundException,
  ResourceNotFoundException,
} from "@aws-sdk/client-cognito-identity-provider";

const cognitoSend = jest.fn();

jest
  .spyOn(CognitoIdentityProviderClient.prototype, "send")
  .mockImplementation(cognitoSend);

import { handler } from "../../lib/lambdas/admin-user-handler";
import { DEFAULT_ADMIN_EMAIL } from "../../lib/constants";

const BASE_EVENT = {
  ServiceToken: "arn:aws:lambda:us-east-1:123456789:function:test",
  StackId: "arn:aws:cloudformation:us-east-1:123456789:stack/test/guid",
  RequestId: "req-123",
  LogicalResourceId: "AdminUser",
  ResourceType: "Custom::AdminUser",
  ResourceProperties: {
    ServiceToken: "arn:aws:lambda:us-east-1:123456789:function:test",
    UserPoolId: "us-east-1_abc123",
    Username: "admin@example.com",
    Email: "admin@example.com",
    Group: "Admin",
  },
};

function makeEvent(
  type: "Create" | "Update" | "Delete",
  extra: Record<string, any> = {},
) {
  return {
    ...BASE_EVENT,
    RequestType: type,
    PhysicalResourceId:
      type === "Create" ? undefined : "us-east-1_abc123/admin@example.com",
    ...extra,
  } as any;
}

beforeEach(() => {
  jest.clearAllMocks();
  cognitoSend.mockResolvedValue({
    User: { Username: "admin@example.com" },
    $metadata: { httpStatusCode: 200 },
  });
});

// ═══════════════════════════════════════════════════════════════════
// Create
// ═══════════════════════════════════════════════════════════════════
describe("Create", () => {
  test("calls Cognito APIs in order (no Secrets Manager, no SetPassword)", async () => {
    await handler(makeEvent("Create"));

    expect(cognitoSend).toHaveBeenCalledTimes(3);
    const calls = cognitoSend.mock.calls.map(
      (c: any[]) => c[0].constructor.name,
    );
    expect(calls).toEqual([
      "AdminCreateUserCommand",
      "CreateGroupCommand",
      "AdminAddUserToGroupCommand",
    ]);
  });

  test("sends temp password via email (DesiredDeliveryMediums)", async () => {
    await handler(makeEvent("Create"));

    const cmd = cognitoSend.mock.calls[0][0];
    expect(cmd.input).toEqual({
      UserPoolId: "us-east-1_abc123",
      Username: "admin@example.com",
      UserAttributes: [
        { Name: "email", Value: "admin@example.com" },
        { Name: "email_verified", Value: "true" },
      ],
      DesiredDeliveryMediums: ["EMAIL"],
    });
  });

  test("does NOT call AdminSetUserPassword", async () => {
    await handler(makeEvent("Create"));

    const calls = cognitoSend.mock.calls.map(
      (c: any[]) => c[0].constructor.name,
    );
    expect(calls).not.toContain("AdminSetUserPasswordCommand");
  });

  test("returns PhysicalResourceId as userPoolId/username", async () => {
    const res = await handler(makeEvent("Create"));
    expect(res.Status).toBe("SUCCESS");
    expect(res.PhysicalResourceId).toBe("us-east-1_abc123/admin@example.com");
  });

  test("throws when AdminCreateUser returns no User", async () => {
    cognitoSend.mockResolvedValueOnce({ User: null });
    await expect(handler(makeEvent("Create"))).rejects.toThrow(
      "Failed to create user",
    );
  });

  test("tolerates UsernameExistsException on AdminCreateUser", async () => {
    cognitoSend
      .mockRejectedValueOnce({ name: "UsernameExistsException" })
      .mockResolvedValueOnce({}) // CreateGroup
      .mockResolvedValueOnce({}); // AdminAddUserToGroup

    const res = await handler(makeEvent("Create"));
    expect(res.Status).toBe("SUCCESS");
  });

  test("tolerates GroupExistsException on CreateGroup", async () => {
    cognitoSend
      .mockResolvedValueOnce({ User: { Username: "admin@example.com" } })
      .mockRejectedValueOnce({ name: "GroupExistsException" })
      .mockResolvedValueOnce({});

    const res = await handler(makeEvent("Create"));
    expect(res.Status).toBe("SUCCESS");
  });
});

// ═══════════════════════════════════════════════════════════════════
// Update
// ═══════════════════════════════════════════════════════════════════
describe("Update", () => {
  test("runs full createUserAndGroup", async () => {
    const res = await handler(makeEvent("Update"));

    expect(cognitoSend).toHaveBeenCalledTimes(3);
    const calls = cognitoSend.mock.calls.map(
      (c: any[]) => c[0].constructor.name,
    );
    expect(calls).toEqual([
      "AdminCreateUserCommand",
      "CreateGroupCommand",
      "AdminAddUserToGroupCommand",
    ]);
    expect(res.Status).toBe("SUCCESS");
  });
});

// ═══════════════════════════════════════════════════════════════════
// Delete
// ═══════════════════════════════════════════════════════════════════
describe("Delete", () => {
  test("deletes user and group", async () => {
    const res = await handler(makeEvent("Delete"));

    expect(cognitoSend).toHaveBeenCalledTimes(2);
    const calls = cognitoSend.mock.calls.map(
      (c: any[]) => c[0].constructor.name,
    );
    expect(calls).toEqual(["AdminDeleteUserCommand", "DeleteGroupCommand"]);
    expect(res.Status).toBe("SUCCESS");
  });

  test("tolerates UserNotFoundException on delete user", async () => {
    cognitoSend
      .mockRejectedValueOnce(
        new UserNotFoundException({ message: "gone", $metadata: {} }),
      )
      .mockResolvedValueOnce({});

    const res = await handler(makeEvent("Delete"));
    expect(res.Status).toBe("SUCCESS");
  });

  test("tolerates ResourceNotFoundException on delete user", async () => {
    cognitoSend
      .mockRejectedValueOnce(
        new ResourceNotFoundException({ message: "gone", $metadata: {} }),
      )
      .mockResolvedValueOnce({});

    const res = await handler(makeEvent("Delete"));
    expect(res.Status).toBe("SUCCESS");
  });

  test("tolerates ResourceNotFoundException on delete group", async () => {
    cognitoSend
      .mockResolvedValueOnce({})
      .mockRejectedValueOnce(
        new ResourceNotFoundException({ message: "gone", $metadata: {} }),
      );

    const res = await handler(makeEvent("Delete"));
    expect(res.Status).toBe("SUCCESS");
  });

  test("rethrows unexpected error on delete user", async () => {
    cognitoSend.mockRejectedValueOnce(new Error("unexpected"));
    await expect(handler(makeEvent("Delete"))).rejects.toThrow("unexpected");
  });

  test("rethrows unexpected error on delete group", async () => {
    cognitoSend
      .mockResolvedValueOnce({})
      .mockRejectedValueOnce(new Error("unexpected"));
    await expect(handler(makeEvent("Delete"))).rejects.toThrow("unexpected");
  });
});

// ═══════════════════════════════════════════════════════════════════
// Validation
// ═══════════════════════════════════════════════════════════════════
describe("validation", () => {
  test("throws on missing UserPoolId", async () => {
    const event = makeEvent("Create", {
      ResourceProperties: {
        ServiceToken: "x",
        Username: "a",
        Email: "a@b.com",
        Group: "G",
      },
    });
    await expect(handler(event)).rejects.toThrow(
      "Missing or invalid UserPoolId",
    );
  });

  test("throws on missing Email", async () => {
    const event = makeEvent("Create", {
      ResourceProperties: {
        ServiceToken: "x",
        UserPoolId: "us-east-1_abc",
        Username: "a",
        Group: "G",
      },
    });
    await expect(handler(event)).rejects.toThrow("Missing or invalid Email");
  });

  test("throws on missing Username", async () => {
    const event = makeEvent("Create", {
      ResourceProperties: {
        ServiceToken: "x",
        UserPoolId: "us-east-1_abc",
        Email: "a@b.com",
        Group: "G",
      },
    });
    await expect(handler(event)).rejects.toThrow("Missing or invalid Username");
  });

  test("throws on missing Group", async () => {
    const event = makeEvent("Create", {
      ResourceProperties: {
        ServiceToken: "x",
        UserPoolId: "us-east-1_abc",
        Username: "a",
        Email: "a@b.com",
      },
    });
    await expect(handler(event)).rejects.toThrow("Missing or invalid Group");
  });
});

// ═══════════════════════════════════════════════════════════════════
// Error handling
// ═══════════════════════════════════════════════════════════════════
describe("error handling", () => {
  test("wraps and rethrows errors with message", async () => {
    cognitoSend.mockRejectedValue(new Error("cognito error"));
    await expect(handler(makeEvent("Create"))).rejects.toThrow("cognito error");
  });

  test("wraps non-Error throws", async () => {
    cognitoSend.mockRejectedValue("string error");
    await expect(handler(makeEvent("Create"))).rejects.toThrow("string error");
  });

  test("throws on unsupported RequestType", async () => {
    await expect(handler(makeEvent("Rollback" as any))).rejects.toThrow(
      "Unsupported CloudFormation request type: Rollback",
    );
  });

  test("throws on invalid email format", async () => {
    const event = makeEvent("Create", {
      ResourceProperties: {
        ServiceToken: "x",
        UserPoolId: "us-east-1_abc",
        Username: "a",
        Email: "not-an-email",
        Group: "G",
      },
    });
    await expect(handler(event)).rejects.toThrow(
      "Email must be a valid email address format",
    );
  });

  test("redacts PII from logged event", async () => {
    const spy = jest.spyOn(console, "log").mockImplementation();
    await handler(makeEvent("Create"));
    const loggedStr = spy.mock.calls[0][1] as string;
    expect(loggedStr).toContain("[REDACTED]");
    expect(loggedStr).not.toContain("admin@example.com");
    spy.mockRestore();
  });
});

// ═══════════════════════════════════════════════════════════════════
// The placeholder admin address is never sent an invite
// ═══════════════════════════════════════════════════════════════════
describe("Create with the default placeholder address", () => {
  const placeholder = {
    ResourceProperties: {
      ...BASE_EVENT.ResourceProperties,
      Username: DEFAULT_ADMIN_EMAIL,
      Email: DEFAULT_ADMIN_EMAIL,
    },
  };

  test("suppresses the invitation to the placeholder address", async () => {
    await handler(makeEvent("Create", placeholder));

    const cmd = cognitoSend.mock.calls[0][0];
    expect(cmd.constructor.name).toBe("AdminCreateUserCommand");
    expect(cmd.input.MessageAction).toBe("SUPPRESS");
    expect(cmd.input.DesiredDeliveryMediums).toBeUndefined();
  });

  test("still creates the admin group and its membership", async () => {
    await handler(makeEvent("Create", placeholder));

    const calls = cognitoSend.mock.calls.map(
      (c: any[]) => c[0].constructor.name,
    );
    expect(calls).toEqual([
      "AdminCreateUserCommand",
      "CreateGroupCommand",
      "AdminAddUserToGroupCommand",
    ]);
  });
});
