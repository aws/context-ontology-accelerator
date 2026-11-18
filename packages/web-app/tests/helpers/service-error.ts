// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { ControlPlaneServiceServiceException } from "@coa/control-plane-client";

/** What the generated client throws for an error response with this status. */
export function serviceError(httpStatusCode: number, message?: string) {
  return new ControlPlaneServiceServiceException({
    name: "Unknown",
    $fault: httpStatusCode < 500 ? "client" : "server",
    $metadata: { httpStatusCode },
    message,
  });
}
