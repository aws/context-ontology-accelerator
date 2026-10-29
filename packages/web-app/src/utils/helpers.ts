// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { AppTranslate } from "../i18n/LocaleProvider";

export const S3_ARN_RE = /^arn:aws:s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/;
export const S3_PREFIX_RE = /^(?!\/)(?!.*\.\.)[A-Za-z0-9_./-]+$/;

export function validateS3Prefix(value: string, t: AppTranslate): string {
  if (!value.trim()) return t("S3 prefix must not be blank.");
  if (value.startsWith("/")) return t("S3 prefix must not start with a slash.");
  if (value.includes("..")) return t('S3 prefix must not contain "..".');
  if (value.startsWith("s3://"))
    return t('S3 prefix must not start with "s3://".');
  if (!S3_PREFIX_RE.test(value))
    return t("S3 prefix contains invalid characters.");
  return "";
}

export function formatTimestamp(date: Date | string | undefined): string {
  if (!date) return "—";
  const d = date instanceof Date ? date : new Date(date);
  return d.toLocaleString();
}
