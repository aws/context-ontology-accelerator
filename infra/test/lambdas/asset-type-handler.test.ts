// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  ConflictException,
  CreateAssetTypeCommand,
  DataZoneClient,
  DeleteAssetTypeCommand,
  GetAssetTypeCommand,
  ResourceNotFoundException,
} from "@aws-sdk/client-datazone";

const dzSend = jest.fn();

jest.spyOn(DataZoneClient.prototype, "send").mockImplementation(dzSend);

import { handler } from "../../lib/lambdas/asset-type-handler";

const PROPS = {
  ServiceToken: "arn:aws:lambda:us-east-1:123456789012:function:cr",
  DomainId: "dzd_test",
  ProjectId: "proj_system",
  Name: "CoaRelationalTable",
  Description: "Relational table asset",
  FormName: "CoaTableMetadata",
  FormRevision: "3",
};

function makeEvent(type: "Create" | "Update" | "Delete"): any {
  return {
    RequestType: type,
    ServiceToken: PROPS.ServiceToken,
    StackId: "arn:aws:cloudformation:us-east-1:123456789012:stack/ns/guid",
    RequestId: "req-1",
    LogicalResourceId: "CoaRelationalTable",
    ResourceType: "AWS::CloudFormation::CustomResource",
    ResourceProperties: PROPS,
    ...(type === "Create"
      ? {}
      : {
          PhysicalResourceId: "dzd_test/CoaRelationalTable",
          OldResourceProperties: { ...PROPS, FormRevision: "2" },
        }),
  };
}

function assetType(formRevision: string, revision: string) {
  return {
    revision,
    formsOutput: {
      CoaTableMetadata: {
        typeName: "CoaTableMetadata",
        typeRevision: formRevision,
        required: false,
      },
    },
  };
}

function conflict() {
  return new ConflictException({ message: "exists", $metadata: {} });
}

function commands(): string[] {
  return dzSend.mock.calls.map((c) => c[0].constructor.name);
}

beforeEach(() => {
  dzSend.mockReset();
});

describe("Create", () => {
  it("creates the asset type pinning the form revision", async () => {
    dzSend.mockResolvedValueOnce({ revision: "1" });

    const res = await handler(makeEvent("Create"));

    expect(res.Status).toBe("SUCCESS");
    expect(res.PhysicalResourceId).toBe("dzd_test/CoaRelationalTable");
    const cmd = dzSend.mock.calls[0][0];
    expect(cmd).toBeInstanceOf(CreateAssetTypeCommand);
    expect(cmd.input.formsInput.CoaTableMetadata).toEqual({
      typeIdentifier: "CoaTableMetadata",
      typeRevision: "3",
      required: false,
    });
  });

  it("keeps treating an existing asset type as success", async () => {
    dzSend.mockRejectedValueOnce(conflict());

    const res = await handler(makeEvent("Create"));

    expect(res.Status).toBe("SUCCESS");
    expect(res.Data).toEqual({ Revision: "existing" });
  });
});

describe("Update", () => {
  it("is a no-op when the asset type already pins the target form revision", async () => {
    dzSend.mockResolvedValueOnce(assetType("3", "4"));

    const res = await handler(makeEvent("Update"));

    expect(res.Status).toBe("SUCCESS");
    expect(res.Data).toEqual({ Revision: "4" });
    expect(commands()).toEqual(["GetAssetTypeCommand"]);
  });

  it("records a new asset-type revision pinning a new form revision", async () => {
    dzSend
      .mockResolvedValueOnce(assetType("2", "1"))
      .mockResolvedValueOnce({ revision: "2" })
      .mockResolvedValueOnce(assetType("3", "2"));

    const res = await handler(makeEvent("Update"));

    expect(res.Status).toBe("SUCCESS");
    expect(res.PhysicalResourceId).toBe("dzd_test/CoaRelationalTable");
    expect(res.Data).toEqual({ Revision: "2" });
    expect(commands()).toEqual([
      "GetAssetTypeCommand",
      "CreateAssetTypeCommand",
      "GetAssetTypeCommand",
    ]);
    const create = dzSend.mock.calls[1][0];
    expect(create.input.name).toBe("CoaRelationalTable");
    expect(create.input.formsInput.CoaTableMetadata.typeRevision).toBe("3");
    expect(dzSend.mock.calls[0][0]).toBeInstanceOf(GetAssetTypeCommand);
  });

  it("fails instead of reporting success when DataZone refuses the new revision", async () => {
    dzSend
      .mockResolvedValueOnce(assetType("2", "1"))
      .mockRejectedValueOnce(conflict());

    await expect(handler(makeEvent("Update"))).rejects.toThrow(
      /pins CoaTableMetadata revision 2.*revision pinning 3/,
    );
  });

  it("fails when the asset type still pins the old revision afterwards", async () => {
    jest.useFakeTimers();
    try {
      dzSend
        .mockResolvedValueOnce(assetType("2", "1"))
        .mockResolvedValueOnce({ revision: "1" })
        .mockResolvedValue(assetType("2", "1"));

      const result = expect(handler(makeEvent("Update"))).rejects.toThrow(
        /still pins CoaTableMetadata revision 2 after update; expected 3/,
      );
      await jest.advanceTimersByTimeAsync(20_000);
      await result;
    } finally {
      jest.useRealTimers();
    }
  });

  it("retries the read-back when GetAssetType lags the create", async () => {
    jest.useFakeTimers();
    try {
      dzSend
        .mockResolvedValueOnce(assetType("2", "1"))
        .mockResolvedValueOnce({ revision: "2" })
        .mockResolvedValueOnce(assetType("2", "1"))
        .mockResolvedValueOnce(assetType("3", "2"));

      const pending = handler(makeEvent("Update"));
      await jest.advanceTimersByTimeAsync(20_000);
      const response = await pending;
      expect(response.Status).toBe("SUCCESS");
    } finally {
      jest.useRealTimers();
    }
  });

  it("creates the asset type when it is missing", async () => {
    dzSend
      .mockRejectedValueOnce(
        new ResourceNotFoundException({ message: "gone", $metadata: {} }),
      )
      .mockResolvedValueOnce({ revision: "1" })
      .mockResolvedValueOnce(assetType("3", "1"));

    const res = await handler(makeEvent("Update"));

    expect(res.Status).toBe("SUCCESS");
    expect(commands()).toEqual([
      "GetAssetTypeCommand",
      "CreateAssetTypeCommand",
      "GetAssetTypeCommand",
    ]);
  });
});

describe("Delete", () => {
  it("deletes best-effort and succeeds", async () => {
    dzSend.mockRejectedValueOnce(new Error("already gone"));

    const res = await handler(makeEvent("Delete"));

    expect(res.Status).toBe("SUCCESS");
    expect(dzSend.mock.calls[0][0]).toBeInstanceOf(DeleteAssetTypeCommand);
  });
});
