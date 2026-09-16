// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";

export function sourceStatusType(
  status: string | undefined,
): StatusIndicatorProps["type"] {
  switch (status) {
    case "COMPLETED":
    case "APPROVED":
      return "success";
    case "SCAN_FAILED":
    case "DELETE_FAILED":
    case "APPROVAL_FAILED":
    case "REJECTION_FAILED":
      return "error";
    case "SCANNING":
    case "ENRICHING":
    case "DELETING":
    case "APPROVING":
    case "REJECTING":
      return "in-progress";
    case "REGISTERED":
      return "pending";
    // A re-scan review is the same kind of state as a first-scan review: the
    // source is waiting on a steward, so it gets the same treatment rather than
    // falling through to the neutral default.
    case "PENDING_REVIEW":
    case "RESCAN_REVIEW":
      return "warning";
    case "DELETED":
    case "REJECTED":
      return "stopped";
    default:
      return "info";
  }
}

export function sourceStatusLabel(status: string | undefined): string {
  switch (status) {
    case "APPROVED":
      return "Approved";
    case "COMPLETED":
      return "Completed";
    case "PENDING_REVIEW":
      return "Pending review";
    case "RESCAN_REVIEW":
      return "Re-scan review";
    case "REGISTERED":
      return "Registered";
    case "SCANNING":
      return "Scanning";
    case "ENRICHING":
      return "Enriching";
    case "APPROVING":
      return "Approving…";
    case "REJECTING":
      return "Rejecting…";
    case "APPROVAL_FAILED":
      return "Approval failed";
    case "REJECTION_FAILED":
      return "Rejection failed";
    case "SCAN_FAILED":
      return "Scan failed";
    case "DELETE_FAILED":
      return "Delete failed";
    case "DELETING":
      return "Deleting";
    case "DELETED":
      return "Deleted";
    case "REJECTED":
      return "Rejected";
    default:
      return status ?? "—";
  }
}
