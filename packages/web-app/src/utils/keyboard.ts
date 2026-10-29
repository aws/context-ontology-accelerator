// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** The parts of Cloudscape's `BaseKeyDetail` this check reads. */
export interface EnterKeyDetail {
  key: string;
  keyCode?: number;
  isComposing?: boolean;
}

/**
 * Key code browsers report for a keydown the IME handles. Safari ends the
 * composition before the confirming keydown, so `isComposing` is already false
 * there and this code is the only sign that the Enter belongs to the IME.
 */
const IME_PROCESS_KEY_CODE = 229;

/**
 * True when Enter should submit or add the value, false for the Enter that
 * confirms an IME conversion (Japanese, Chinese, Korean). Without this check,
 * confirming the first word of a Japanese question in the Playground sends a
 * question that is only that word.
 */
export function isSubmitEnter(detail: EnterKeyDetail): boolean {
  if (detail.key !== "Enter") return false;
  if (detail.isComposing) return false;
  return detail.keyCode !== IME_PROCESS_KEY_CODE;
}
