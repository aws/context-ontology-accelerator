// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// The console renders these API values and validation messages through the
// catalogue, so each one must read as Japanese when the UI language is ja and
// keep its English wording when it is en.

import { describe, expect, it } from "vitest";
import { ApiError } from "@components/ApiClientProvider";
import {
  formatAppMessage,
  formatEnglishMessage,
  type AppMessage,
  type AppMessageValues,
} from "./appMessages";
import type { AppTranslate, SupportedLocale } from "./LocaleProvider";
import {
  localizeNamespaceStatus,
  localizeVkgHealth,
} from "@utils/namespace-status";
import { localizePrincipalType } from "@utils/principal-type";
import { localizeOntologyType } from "@utils/ontology-display";
import {
  validateClassification,
  validateTopic,
} from "@utils/extraction-vocabulary";
import { validateS3Prefix } from "@utils/helpers";
import { localizeTraceStatus } from "@components/Chat/step-labels";
import { describePlaygroundError } from "@components/Chat/playground-errors";
import { proposalStatusText } from "../pages/OntologyPage";
import { describeSaveFailure } from "../pages/ontology/induce/ProposalDetail";

function translator(locale: SupportedLocale): AppTranslate {
  return (message: AppMessage, values?: AppMessageValues) =>
    formatAppMessage(locale, message, values);
}

const en = translator("en");
const ja = translator("ja");

describe("namespace and VKG status", () => {
  it("names each namespace status in both languages", () => {
    expect(localizeNamespaceStatus("ACTIVE", en)).toBe("Active");
    expect(localizeNamespaceStatus("ACTIVE", ja)).toBe("アクティブ");
    expect(localizeNamespaceStatus("DELETE_FAILED", ja)).toBe("削除失敗");
  });

  it("keeps an unknown status raw and an absent one as a dash", () => {
    expect(localizeNamespaceStatus("SOMETHING_NEW", ja)).toBe("SOMETHING_NEW");
    expect(localizeNamespaceStatus(undefined, ja)).toBe("—");
  });

  it("names each VKG health status in both languages", () => {
    expect(localizeVkgHealth("DEGRADED", en)).toBe("Degraded");
    expect(localizeVkgHealth("DEGRADED", ja)).toBe("機能低下");
    expect(localizeVkgHealth("UNKNOWN", ja)).toBe("不明");
    expect(localizeVkgHealth("UNAVAILABLE", ja)).toBe("利用不可");
    expect(localizeVkgHealth("PROVISIONING", ja)).toBe("準備中");
    expect(localizeVkgHealth("HEALTHY", ja)).toBe("正常");
  });
});

describe("principal and ontology type", () => {
  it("names User and Group in both languages", () => {
    expect(localizePrincipalType("User", en)).toBe("User");
    expect(localizePrincipalType("User", ja)).toBe("ユーザー");
    expect(localizePrincipalType("Group", ja)).toBe("グループ");
    expect(localizePrincipalType(undefined, ja)).toBe("—");
  });

  it("names the API ontology types in both languages", () => {
    expect(localizeOntologyType("user_created", en)).toBe("Uploaded");
    expect(localizeOntologyType("user_created", ja)).toBe(
      formatAppMessage("ja", "Uploaded"),
    );
    expect(localizeOntologyType("induced", ja)).toBe(
      formatAppMessage("ja", "Induced"),
    );
    expect(localizeOntologyType("user_created", ja)).not.toBe("user_created");
  });
});

describe("trace step status", () => {
  it("names allow and the other recorded statuses", () => {
    expect(localizeTraceStatus(en, "allow")).toBe("Resolved");
    expect(localizeTraceStatus(ja, "allow")).toBe("解決済み");
    expect(localizeTraceStatus(ja, "succeeded")).toBe("解決済み");
    expect(localizeTraceStatus(ja, "timeout")).toBe("タイムアウト");
    expect(localizeTraceStatus(ja, "denied")).toBe("拒否");
  });
});

describe("proposal status", () => {
  it("names a cancelled proposal in both languages", () => {
    expect(proposalStatusText("cancelled", en)).toBe("cancelled");
    expect(proposalStatusText("cancelled", ja)).toBe("キャンセル済み");
    expect(proposalStatusText("pending", ja)).toBe("待機中");
  });
});

describe("Playground errors", () => {
  it("shows the access deny in Japanese without a retry", () => {
    const text = describePlaygroundError(
      "AccessDenied",
      "Access denied",
      "ja",
      ja,
    );
    expect(text.summary).toBe("アクセスが拒否されました");
    expect(text.detail).toBeUndefined();
    expect(text.retryable).toBe(false);
  });

  it("keeps the English server sentence as the English headline", () => {
    const text = describePlaygroundError(
      "AccessDenied",
      "Access denied",
      "en",
      en,
    );
    expect(text).toEqual({ summary: "Access denied", retryable: false });
  });

  it("keeps the server's detail under the Japanese summary", () => {
    const server =
      "table reference 'customers' is ambiguous: it could be any of customers";
    const text = describePlaygroundError(
      "AmbiguousReferenceError",
      server,
      "ja",
      ja,
    );
    expect(text.summary).toBe(
      "質問内のテーブルが複数のデータソースに一致します",
    );
    expect(text.detail).toBe(server);
  });

  it("maps the timeout and internal errors and lets them be retried", () => {
    const timeout = describePlaygroundError(
      "TimeoutError",
      "Query resolution timed out",
      "ja",
      ja,
    );
    expect(timeout.summary).toBe("質問の処理がタイムアウトしました");
    expect(timeout.retryable).toBe(true);
    expect(
      describePlaygroundError(
        "InternalError",
        "Query resolution failed",
        "ja",
        ja,
      ).summary,
    ).toBe("質問を処理できませんでした");
  });

  it("uses the catalogue's wording for transport failures in Japanese", () => {
    expect(describePlaygroundError("StreamError", "", "ja", ja).summary).toBe(
      "リクエストに失敗しました",
    );
    expect(
      describePlaygroundError("InvalidResponse", "", "ja", ja).summary,
    ).toBe("サーバーの応答の形式が正しくありません");
    expect(ja("Choose which edits to save")).toBe(
      "保存する編集を選択してください",
    );
  });

  it("falls back to the server text for an unknown type", () => {
    expect(
      describePlaygroundError("BrandNewError", "Something odd", "ja", ja),
    ).toEqual({ summary: "Something odd", retryable: true });
  });
});

describe("validation messages", () => {
  it("validates extraction labels and topics in both languages", () => {
    expect(validateClassification(" ", [], en)).toBe("Enter a label.");
    expect(validateClassification(" ", [], ja)).toBe(
      "ラベルを入力してください。",
    );
    expect(validateClassification("Dress", ["dress"], ja)).toBe(
      "「Dress」はすでに追加したラベルと重複しています。",
    );
    expect(validateClassification("x".repeat(129), [], ja)).toBe(
      "ラベルは 128 文字以内で入力してください。",
    );
    expect(validateTopic(" ", [], ja)).toBe("トピック名を入力してください。");
    expect(validateTopic("Gala", ["gala"], ja)).toBe(
      "「Gala」はすでに追加したトピックと重複しています。",
    );
  });

  it("keeps the English wording with the English formatter", () => {
    expect(
      validateClassification("x".repeat(129), [], formatEnglishMessage),
    ).toBe("Labels must be 128 characters or fewer.");
    expect(validateS3Prefix("/docs", formatEnglishMessage)).toBe(
      "S3 prefix must not start with a slash.",
    );
  });

  it("validates S3 prefixes in Japanese", () => {
    expect(validateS3Prefix("/docs", ja)).toBe(
      "S3 プレフィックスをスラッシュで始めることはできません。",
    );
    expect(validateS3Prefix("a/../b", ja)).toBe(
      "S3 プレフィックスに「..」を含めることはできません。",
    );
    expect(validateS3Prefix("s3://bucket/x", ja)).toBe(
      "S3 プレフィックスを「s3://」で始めることはできません。",
    );
    expect(validateS3Prefix("docs?", ja)).toBe(
      "S3 プレフィックスに使用できない文字が含まれています。",
    );
    expect(validateS3Prefix("docs/2026", ja)).toBe("");
  });
});

describe("proposal save failures", () => {
  it("explains a refused save in Japanese and keeps the server sentence", () => {
    const error = new ApiError(409, "Conflict", "Proposal is not editable.");
    expect(describeSaveFailure(error, ja)).toBe(
      "サーバーがこの保存を拒否しました: Proposal is not editable. 変更せずにやり直しても同じように失敗します。",
    );
  });

  it("keeps the English wording with the English formatter", () => {
    const error = new ApiError(503, "ServiceUnavailable", "Store unavailable.");
    expect(describeSaveFailure(error, formatEnglishMessage)).toBe(
      "Store unavailable. Nothing was saved, so you can retry this save as-is.",
    );
    expect(describeSaveFailure("odd", ja)).toBe(
      "提案の変更を保存できませんでした",
    );
  });
});
