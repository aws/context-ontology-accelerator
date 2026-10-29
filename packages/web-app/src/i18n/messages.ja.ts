// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Japanese message catalogue (data only).
 *
 * Every UI string the app renders must have an entry here; t(...) will not
 * compile without it; keep placeholders identical to the English key.
 *
 * The keys are the English copy verbatim (see ./appMessages for why English is
 * the key and for the formatter/type wiring). To add another locale, create a
 * sibling messages.<locale>.ts and register it in CATALOGUES — TypeScript then
 * requires it to cover every key.
 */
export const MESSAGES_JA = {
  Info: "情報",
  "Close info": "情報を閉じる",
  Scan: "スキャン",
  Sources: "データソース",
  Ontology: "オントロジー",
  Induction: "誘導",
  Explorer: "エクスプローラー",
  Metrics: "メトリクス",
  Home: "ホーム",
  Serve: "提供",
  Playground: "プレイグラウンド",
  "MCP Server": "MCP サーバー",
  Administration: "管理",
  Namespaces: "名前空間",
  Identity: "ID 管理",
  "System Health": "システム状態",
  // Breadcrumb labels for the last crumb of create / connect pages.
  Create: "作成",
  Connect: "接続",
  "Namespace: {name}": "名前空間: {name}",
  Loading: "読み込み中",
  "Namespace switcher": "名前空間の切り替え",
  Namespace: "名前空間",
  "No namespaces — create one first":
    "名前空間がありません — 先に作成してください",
  "Switch to light mode": "ライトモードに切り替え",
  "Switch to dark mode": "ダークモードに切り替え",
  "Light mode": "ライトモード",
  "Dark mode": "ダークモード",
  Settings: "設定",
  "Switch to comfortable density": "標準の表示密度に切り替え",
  "Switch to compact density": "コンパクト表示に切り替え",
  Language: "言語",
  "English (en)": "英語 (en)",
  "Japanese (ja)": "日本語 (ja)",
  "Namespace language": "名前空間の言語",
  "Set when the namespace was created and cannot be changed.":
    "名前空間の作成時に決まり、変更できません。",
  "Text COA generates (descriptions, synonyms, and labels for document sources) uses this language. Source text and table and column names are kept as-is.":
    "COA が生成するテキスト（説明文、同義語、ドキュメントソースのラベル）はこの言語で作られます。ソースの本文と、テーブル名・列名はそのまま使われます。",
  "The language cannot be changed after the namespace is created, and it must match the language of the sources you connect. To use a different language, create a new namespace.":
    "言語は名前空間の作成後に変更できず、接続するソースの言語と一致している必要があります。別の言語を使うには、新しい名前空間を作成してください。",
  "Confirm namespace language": "名前空間の言語の確認",
  "Confirm and create": "確認して作成",
  "The language cannot be changed": "言語は変更できません",
  "This namespace will be created in this language. The language cannot be changed later, and it must match the language of the sources you connect. To use a different language, you must create a new namespace.":
    "この名前空間はこの言語で作成されます。言語は後から変更できず、接続するソースの言語と一致している必要があります。別の言語を使うには、新しい名前空間を作成する必要があります。",
  "User menu": "ユーザーメニュー",
  Profile: "プロフィール",
  "Sign out": "サインアウト",
  Breadcrumbs: "パンくずリスト",
  "Please select or create a namespace first.":
    "先に名前空間を選択または作成してください。",
  "Get started": "開始する",
  "Close Get started": "「開始する」を閉じる",
  "AI Platform · Context Layer": "AI プラットフォーム · コンテキスト層",
  "The governed context layer for enterprise AI.":
    "エンタープライズ AI のためのガバナンスされたコンテキスト層。",
  "Connect to your Glue and SageMaker catalogs, induce a W3C-standard semantic model, and serve it to analysts and AI agents over MCP.":
    "Glue と SageMaker のカタログに接続し、W3C 標準のセマンティックモデルを誘導して、MCP 経由でアナリストや AI エージェントに提供します。",
  "Welcome, {name}.": "ようこそ、{name} さん。",
  "Create a namespace and connect your first source in a few guided steps.":
    "ガイドに沿って名前空間を作成し、最初のデータソースを接続します。",
  "View namespaces": "名前空間を表示",
  "How it works": "仕組み",
  "Point Context Ontology Accelerator at your Glue and SageMaker catalogs. It inventories tables and columns, then previews AI-generated business context for your review.":
    "Context Ontology Accelerator を Glue と SageMaker のカタログに接続します。テーブルと列を収集し、AI が生成した業務コンテキストをレビュー用に提示します。",
  "Draft a W3C-standard ontology over scanned catalogs. Author validation rules, define governed metrics, and publish through a versioned workflow.":
    "スキャンしたカタログから W3C 標準のオントロジーを作成します。検証ルールとガバナンス対象のメトリクスを定義し、バージョン管理されたワークフローで公開します。",
  "Expose the governed surface to analysts and agents. Every request flows through policy enforcement, with every decision logged.":
    "ガバナンスされた情報をアナリストやエージェントに提供します。すべてのリクエストにポリシーを適用し、判断を記録します。",
  Policy: "保険契約",
  Coverage: "補償",
  Party: "関係者",
  Claim: "保険金請求",
  Payment: "支払い",
  Agent: "代理店",
  Endorsement: "契約変更",
  "Choose your domain": "対象ドメインを選ぶ",
  "Pick the business area you want to model first and what you intend to use it for. You can always expand scope later.":
    "最初にモデル化する業務領域と利用目的を選びます。範囲は後から拡張できます。",
  "Connect a data source": "データソースを接続する",
  "Point Context Ontology Accelerator at a Glue catalog, database, or upload a file. It scans your schema and generates a draft set of concepts automatically.":
    "Glue カタログやデータベースに接続するか、ファイルをアップロードします。スキーマをスキャンし、概念の草案を自動生成します。",
  "Curate induced concepts": "誘導された概念を整理する",
  "Review what Context Ontology Accelerator found — accept, reject, merge, or refine proposed concepts before they go live. Work through a few at a time.":
    "Context Ontology Accelerator が見つけた候補を確認し、公開前に承認、却下、統合、修正します。少しずつ進められます。",
  "Test with a question": "質問でテストする",
  "Ask a natural-language question and see how your ontology grounds the answer. This is your proof of value — even a few concepts make a difference.":
    "自然言語で質問し、オントロジーが回答をどのように根拠付けるか確認します。少数の概念でも効果を確認できます。",
  "Set up your governed context layer in a few steps.":
    "数ステップでガバナンスされたコンテキスト層を構築します。",
  "Sign in": "サインイン",
  "Sign in failed. Please try again.":
    "サインインに失敗しました。もう一度お試しください。",
  Standard: "標準",
  "Deep Reasoning": "深い推論",
  "The SPARQL and SQL Deep Reasoning generated and {engine} executed.":
    "深い推論が生成し、{engine} が実行した SPARQL と SQL。",
  "Execution mode": "実行モード",
  "New chat": "新しいチャット",
  Rationale: "根拠",
  "Query endpoint not configured. Add serveRuntimeArn to runtime-config.json.":
    "クエリエンドポイントが設定されていません。runtime-config.json に serveRuntimeArn を追加してください。",
  "No namespace selected. Choose one from the top navigation.":
    "名前空間が選択されていません。上部ナビゲーションから選択してください。",
  "Previous session history may not have loaded":
    "以前のセッション履歴を読み込めなかった可能性があります",
  Connecting: "接続中",
  "Restoring session": "セッションを復元中",
  "Loading earlier messages": "以前のメッセージを読み込み中",
  "Load earlier messages": "以前のメッセージを読み込む",
  "Learn more": "詳細",
  "Context Ontology Accelerator on GitHub":
    "GitHub の Context Ontology Accelerator",
  "Ask a business question in plain language. Context Ontology Accelerator routes through three tiers — cheapest first — and falls through gracefully when a tier can't answer.":
    "業務上の質問を自然な言葉で入力します。Context Ontology Accelerator は低コストなものから 3 つの層を順に使い、ある層で回答できない場合は次の層へ移ります。",
  "Three-tier resolution": "3 層の解決方式",
  "Tier 1 — metric.": "Tier 1 — メトリクス。",
  "Synonym table → pre-compiled SQL → Athena. Zero LLM calls. Deterministic; the number a CFO sees in a dashboard equals the number an agent returns over MCP.":
    "同義語テーブル → 事前コンパイル済み SQL → Athena。LLM 呼び出しはありません。決定的に動作し、ダッシュボードの数値と MCP 経由でエージェントが返す数値が一致します。",
  "Tier 2 — ontology.": "Tier 2 — オントロジー。",
  "NL → SPARQL (LLM) → VKG → SQL → Athena. Grounded in the formal data model, not table-name guessing.":
    "自然言語 → SPARQL（LLM）→ VKG → SQL → Athena。テーブル名の推測ではなく、正式なデータモデルに根拠を置きます。",
  "Tier 3 — knowledge.": "Tier 3 — ナレッジ。",
  "Parallel vector + graph retrieval → LLM synthesis. Always cites which documents informed the answer.":
    "ベクトル検索とグラフ検索を並列実行 → LLM で合成。回答の根拠となった文書を常に引用します。",
  "Boundary controls (cross-cutting)": "境界制御（全層共通）",
  "authorize on (principal, action, resource) before any tier runs.":
    "各層を実行する前に（principal、action、resource）で認可します。",
  "rewrites compiled SQL with Cedar-derived row filters.":
    "コンパイル済み SQL に Cedar 由来の行フィルターを適用します。",
  "content filter on prompts and outputs.":
    "プロンプトと出力にコンテンツフィルターを適用します。",
  "Demos to try": "試せるデモ",
  Status: "ステータス",
  Created: "作成日時",
  Updated: "更新日時",
  Active: "アクティブ",
  Archived: "アーカイブ済み",
  Deleting: "削除中",
  Deleted: "削除済み",
  "Delete failed": "削除失敗",
  "All statuses": "すべてのステータス",
  Activate: "有効化",
  Archive: "アーカイブ",
  Delete: "削除",
  selected: "選択済み",
  "No namespaces": "名前空間がありません",
  "Create your first namespace to start building a semantic context.":
    "最初の名前空間を作成して、セマンティックコンテキストの構築を始めます。",
  "Create namespace": "名前空間を作成",
  "No matches": "一致する項目がありません",
  "No namespaces matched the filter criteria.":
    "フィルター条件に一致する名前空間はありません。",
  "Namespace deletion blocked": "名前空間の削除がブロックされました",
  "Loading namespaces": "名前空間を読み込み中",
  "Namespace selection": "名前空間の選択",
  "{count} namespaces selected": "{count} 件の名前空間を選択中",
  "Select {name}": "{name} を選択",
  "Displaying namespaces {first} to {last} of {total}":
    "名前空間 {first}〜{last} 件を表示（全 {total} 件）",
  "Namespaces partition the semantic graph into isolated scopes. Each namespace has its own data sources, ontology, and access policies.":
    "名前空間はセマンティックグラフを独立したスコープに分割します。各名前空間は固有のデータソース、オントロジー、アクセスポリシーを持ちます。",
  Refresh: "更新",
  "Change status": "ステータスを変更",
  "Status update failed: {message}":
    "ステータスの更新に失敗しました: {message}",
  "This action is unavailable for the current status.":
    "現在のステータスではこの操作を実行できません。",
  "Filter namespaces": "名前空間をフィルター",
  "Find namespaces": "名前空間を検索",
  "{count} matches": "{count} 件一致",
  "Filter by status": "ステータスでフィルター",
  Preferences: "設定",
  Confirm: "確認",
  Cancel: "キャンセル",
  "Page size": "ページサイズ",
  "10 namespaces": "10 件",
  "20 namespaces": "20 件",
  "50 namespaces": "50 件",
  "Wrap lines": "行を折り返す",
  "Wrap long text to multiple lines.": "長いテキストを複数行に折り返します。",
  "Striped rows": "行を縞表示",
  "Alternate row background.": "行の背景色を交互に変えます。",
  Density: "表示密度",
  "Use compact density for scanning.":
    "一覧を確認しやすいコンパクト表示を使用します。",
  "Column preferences": "列の設定",
  "Customize column visibility and order.":
    "列の表示・非表示と順序を設定します。",
  "Delete namespace “{name}”?": "名前空間「{name}」を削除しますか？",
  "This action cannot be undone": "この操作は元に戻せません",
  "Deleting this namespace permanently removes all of its resources:":
    "この名前空間を削除すると、次のリソースが完全に削除されます:",
  "All data sources and their discovered metadata":
    "すべてのデータソースと検出されたメタデータ",
  "All metric definitions": "すべてのメトリクス定義",
  "All ontology data (classes, properties, induced graphs)":
    "すべてのオントロジーデータ（クラス、プロパティ、誘導グラフ）",
  "Role and permission grants scoped to this namespace":
    "この名前空間に限定されたロールと権限の付与",
  "The Athena workgroup and DataZone project":
    "Athena ワークグループと DataZone プロジェクト",
  "Deletion runs in the background. The namespace remains in Deleting status until it completes, and deleted data cannot be recovered.":
    "削除はバックグラウンドで実行されます。完了するまで名前空間は削除中の状態になり、削除したデータは復元できません。",
  "Type “{name}” to confirm": "確認のため「{name}」と入力",
  "Namespace name is required.": "名前空間名は必須です。",
  "Name must start with a lowercase letter and contain only lowercase letters, numbers, and hyphens.":
    "名前は小文字で始め、小文字、数字、ハイフンのみを使用してください。",
  "Create a new namespace to partition your semantic graph into an isolated scope.":
    "セマンティックグラフを独立したスコープに分割する新しい名前空間を作成します。",
  Unauthorized: "権限がありません",
  "Error creating namespace": "名前空間の作成エラー",
  "Namespace details": "名前空間の詳細",
  "Namespace name": "名前空間名",
  "Machine-friendly identifier: {name}": "システム用の識別子: {name}",
  "Description - optional": "説明（任意）",
  "A brief description of the namespace purpose.":
    "名前空間の目的を簡潔に説明します。",
  "e.g., Insurance Prod": "例: 保険本番",
  "e.g., Production namespace for insurance domain data":
    "例: 保険ドメインデータの本番用名前空間",
  "Failed to load namespace: {message}":
    "名前空間を読み込めませんでした: {message}",
  "Namespace not found": "名前空間が見つかりません",
  Edit: "編集",
  Permissions: "権限",
  Summary: "概要",
  "Namespace ID": "名前空間 ID",
  Name: "名前",
  Owner: "所有者",
  "VKG health": "VKG の状態",
  Unavailable: "利用不可",
  Provisioning: "準備中",
  "The VKG translation service is running and able to translate queries.":
    "VKG 変換サービスは稼働中で、クエリを変換できます。",
  "A VKG task is running but its health check is failing, so it cannot translate queries (for example, the published mapping could not be loaded). Queries will fail — check the container's health status.":
    "VKG タスクは稼働していますがヘルスチェックに失敗しているため、クエリを変換できません（公開済みマッピングを読み込めない場合など）。クエリは失敗します。コンテナのヘルス状態を確認してください。",
  "The VKG service exists but is not serving — queries will fail.":
    "VKG サービスは存在しますが提供できない状態のため、クエリは失敗します。",
  "The VKG service is starting up (deployment in progress) and is expected to become healthy shortly.":
    "VKG サービスを起動中です（デプロイ進行中）。まもなく正常になる見込みです。",
  "No VKG service is deployed yet for this namespace, or its state could not be determined.":
    "この名前空間には VKG サービスがまだデプロイされていないか、状態を確認できません。",
  Description: "説明",
  Ontologies: "オントロジー",
  "Source name": "データソース名",
  Type: "種類",
  Database: "データベース",
  Tables: "テーブル",
  "Loading sources…": "データソースを読み込み中…",
  "Failed to load sources: {message}":
    "データソースを読み込めませんでした: {message}",
  "No sources in this namespace.": "この名前空間にはデータソースがありません。",
  "Failed to load ontologies": "オントロジーを読み込めませんでした",
  Classes: "クラス",
  Properties: "プロパティ",
  "Loading ontologies…": "オントロジーを読み込み中…",
  "Failed to load ontologies: {message}":
    "オントロジーを読み込めませんでした: {message}",
  "No ontologies in this namespace yet.":
    "この名前空間にはまだオントロジーがありません。",
  Metric: "メトリクス",
  "Data source": "データソース",
  Table: "テーブル",
  "Loading metrics…": "メトリクスを読み込み中…",
  "Failed to load metrics: {message}":
    "メトリクスを読み込めませんでした: {message}",
  "No metrics in this namespace yet.":
    "この名前空間にはまだメトリクスがありません。",
  "Failed to update namespace": "名前空間を更新できませんでした",
  "Edit namespace": "名前空間を編集",
  Save: "保存",
  "Display name": "表示名",
  "Back to namespace": "名前空間に戻る",
  "{name} — Permissions": "{name} — 権限",
  Scope: "スコープ",
  "Built-in": "組み込み",
  Yes: "はい",
  No: "いいえ",
  None: "なし",
  All: "すべて",
  "Failed to load members: {message}":
    "メンバーを読み込めませんでした: {message}",
  "No members in this namespace.": "この名前空間にはメンバーがいません。",
  "No members match the filter.": "フィルターに一致するメンバーはいません。",
  "Failed to revoke {failed} of {total}: {principals}":
    "{total} 件中 {failed} 件の権限を取り消せませんでした: {principals}",
  "Members ({count})": "メンバー（{count}）",
  "Loading members…": "メンバーを読み込み中…",
  "Namespace members": "名前空間のメンバー",
  "Member selection": "メンバーの選択",
  "{count} members selected": "{count} 件のメンバーを選択中",
  "Select grant for {principal}": "{principal} の権限付与を選択",
  Revoke: "取り消す",
  "Grant access": "アクセス権を付与",
  "Users with access to this namespace": "この名前空間にアクセスできるユーザー",
  "Filter members": "メンバーをフィルター",
  "Find members": "メンバーを検索",
  "Roles ({count})": "ロール（{count}）",
  "Loading roles…": "ロールを読み込み中…",
  "Namespace roles": "名前空間のロール",
  "Failed to load roles: {message}": "ロールを読み込めませんでした: {message}",
  "No roles defined for this namespace.":
    "この名前空間に定義されたロールはありません。",
  "Confirm revoke": "権限取り消しの確認",
  "Are you sure you want to revoke access for {count} principals?":
    "{count} 件のプリンシパルのアクセス権を取り消しますか？",
  Principal: "プリンシパル",
  "Granted by": "付与者",
  "Granted at": "付与日時",
  "Allowed tables": "許可されたテーブル",
  "Allowed metrics": "許可されたメトリクス",
  "Denied columns": "拒否された列",
  User: "ユーザー",
  Group: "グループ",
  Grant: "付与",
  Email: "メールアドレス",
  "Group name": "グループ名",
  "Principal ID is required": "プリンシパル ID は必須です",
  "Please enter a valid email address":
    "有効なメールアドレスを入力してください",
  "Group name may only contain letters, numbers, hyphens, underscores, and dots":
    "グループ名には英字、数字、ハイフン、アンダースコア、ドットのみ使用できます",
  "Failed to create grant": "権限を付与できませんでした",
  "Failed to load roles": "ロールを読み込めませんでした",
  "Select a role": "ロールを選択",
  "No roles available": "利用可能なロールがありません",
  "Table allowlist": "テーブル許可リスト",
  "Optional. Tables the principal may query. Add one per chip; blank means no table restriction.":
    "任意。プリンシパルがクエリできるテーブルです。チップごとに 1 件追加します。空の場合はテーブル制限を設けません。",
  "Column denylist": "列拒否リスト",
  "Optional. Per table, the columns the principal may not read.":
    "任意。テーブルごとに、プリンシパルが読み取れない列を指定します。",
  "Optional. Metrics the principal may read. Add one per chip.":
    "任意。プリンシパルが読み取れるメトリクスです。チップごとに 1 件追加します。",
  Glue: "Glue",
  JDBC: "JDBC",
  S3: "S3",
  "Custom connector": "カスタムコネクタ",
  Databricks: "Databricks",
  Upload: "アップロード",
  "10 sources": "10 件",
  "20 sources": "20 件",
  "50 sources": "50 件",
  "Source properties": "データソースのプロパティ",
  "Last updated": "最終更新",
  Approved: "承認済み",
  Completed: "完了",
  "Pending review": "レビュー待ち",
  "Re-scan review": "再スキャンのレビュー",
  Registered: "登録済み",
  Scanning: "スキャン中",
  Enriching: "エンリッチ中",
  "Approving…": "承認中…",
  "Rejecting…": "却下中…",
  "Approval failed": "承認失敗",
  "Rejection failed": "却下失敗",
  "Scan failed": "スキャン失敗",
  Rejected: "却下済み",
  "No sources": "データソースがありません",
  "Connect a database or document source to get started.":
    "データベースまたは文書データソースを接続して開始します。",
  "Connect source": "データソースを接続",
  "Failed to load sources": "データソースを読み込めませんでした",
  "Loading sources": "データソースを読み込み中",
  "All sources registered in this namespace.":
    "この名前空間に登録されたすべてのデータソースです。",
  "Find sources": "データソースを検索",
  "Select visible columns": "表示する列を選択",
  "Wrap long text in table cells": "テーブルセル内の長いテキストを折り返します",
  "Alternate row background colors": "行の背景色を交互に変えます",
  "Source selection": "データソースの選択",
  "Metric “{name}” deleted.": "メトリクス「{name}」を削除しました。",
  "Failed to load metric: {message}":
    "メトリクスを読み込めませんでした: {message}",
  "Metric not found.": "メトリクスが見つかりません。",
  Unit: "単位",
  "Return type": "戻り値の型",
  "Defined by": "定義者",
  "Effective from": "有効開始日",
  "Default time grain": "既定の時間粒度",
  Source: "データソース",
  "Source table": "ソーステーブル",
  "Time grain": "時間粒度",
  Expressions: "式",
  Dialect: "SQL 方言",
  "SQL Expression": "SQL 式",
  "AI Context": "AI コンテキスト",
  Synonyms: "同義語",
  Instructions: "指示",
  Examples: "例",
  "Ontology Links": "オントロジーとのリンク",
  "No ontology classes linked. Edit this metric to associate it with classes from your ontology.":
    "オントロジークラスがリンクされていません。このメトリクスを編集して、オントロジーのクラスに関連付けてください。",
  "Delete metric": "メトリクスを削除",
  "Permanently delete metric {name}?":
    "メトリクス「{name}」を完全に削除しますか？",
  "This action cannot be undone. The metric will be removed from the ontology graph and will no longer be discoverable.":
    "この操作は元に戻せません。メトリクスはオントロジーグラフから削除され、検索できなくなります。",
  Day: "日",
  Week: "週",
  Month: "月",
  Quarter: "四半期",
  Year: "年",
  "Failed to search ontology classes":
    "オントロジークラスを検索できませんでした",
  "Name is required": "名前は必須です",
  "Description is required": "説明は必須です",
  "Data source is required": "データソースは必須です",
  "Source table is required": "ソーステーブルは必須です",
  "At least one dialect expression is required":
    "1 つ以上の SQL 方言の式が必要です",
  "Edit {name}": "{name} を編集",
  "Create metric": "メトリクスを作成",
  "Save changes": "変更を保存",
  "Basic information": "基本情報",
  "Alphanumeric and underscores, 1-128 characters":
    "英数字とアンダースコア、1〜128 文字",
  monthly_revenue: "monthly_revenue",
  "Total revenue from all orders in a calendar month":
    "暦月内のすべての注文による総売上",
  "Select a data source": "データソースを選択",
  "No sources available": "利用可能なデータソースがありません",
  "Select a table": "テーブルを選択",
  "Loading tables": "テーブルを読み込み中",
  "No tables found for this source":
    "このデータソースにはテーブルが見つかりません",
  "Select a data source first": "先にデータソースを選択してください",
  "Default time grain (optional)": "既定の時間粒度（任意）",
  "Default time granularity for aggregation (e.g., DAY, MONTH). Used when no grain is specified in a query.":
    "集計に使う既定の時間粒度（例: DAY、MONTH）。クエリで粒度が指定されていない場合に使用します。",
  "Unit (optional)": "単位（任意）",
  "Unit of measurement for the metric result (e.g., USD, claims, percentage).":
    "メトリクス結果の測定単位（例: USD、件数、パーセント）。",
  currency: "通貨",
  "Return type (optional)": "戻り値の型（任意）",
  "Data type returned by the expression (e.g., INTEGER, DECIMAL, FLOAT).":
    "式が返すデータ型（例: INTEGER、DECIMAL、FLOAT）。",
  "xsd:decimal": "xsd:decimal",
  "SQL expression to compute this metric. Add one per SQL dialect your sources use.":
    "このメトリクスを計算する SQL 式です。データソースで使用する SQL 方言ごとに 1 つ追加してください。",
  "Add dialect": "SQL 方言を追加",
  Remove: "削除",
  "SUM(orders.total_amount)": "SUM(orders.total_amount)",
  "AI Context (optional)": "AI コンテキスト（任意）",
  "Alternative names users might use when asking about this metric.":
    "このメトリクスについて質問するときに利用者が使う可能性がある別名です。",
  "Add synonym and press Enter": "同義語を入力して Enter キーを押します",
  Add: "追加",
  "Guidance for the AI on when and how to use this metric.":
    "このメトリクスをいつ、どのように使うかを AI に伝える指示です。",
  "Use with order_date as time dimension.":
    "order_date を時間ディメンションとして使用します。",
  "Example natural language questions this metric answers.":
    "このメトリクスで回答できる自然言語の質問例です。",
  "Add example": "例を追加",
  Question: "質問",
  "What was the total revenue last month?": "先月の総売上はいくらですか？",
  "Ontology Links (optional)": "オントロジーとのリンク（任意）",
  "Ontology classes": "オントロジークラス",
  "Search for classes from your ontology to link to this metric.":
    "このメトリクスにリンクするクラスをオントロジーから検索します。",
  "Type to search classes…": "クラスを検索する文字列を入力…",
  "No classes found": "クラスが見つかりません",
  "Searching…": "検索中…",
  "Use: “{value}”": "「{value}」を使用",
  'Use: "{value}"': "「{value}」を使用",
  "20 metrics": "20 件",
  "50 metrics": "50 件",
  "100 metrics": "100 件",
  Dialects: "SQL 方言",
  "Name values": "名前の値",
  "Data source values": "データソースの値",
  "Source table values": "ソーステーブルの値",
  "Defined by values": "定義者の値",
  "No metrics": "メトリクスがありません",
  "No metrics have been defined yet. Import an OSI file to get started.":
    "メトリクスはまだ定義されていません。OSI ファイルをインポートして開始してください。",
  "Import OSI": "OSI をインポート",
  "No metrics matched the filter criteria.":
    "フィルター条件に一致するメトリクスはありません。",
  "Failed to load all metrics for the filtered operation: {message}":
    "フィルター対象の操作に必要なすべてのメトリクスを読み込めませんでした: {message}",
  "Unknown error": "不明なエラー",
  "Import started. Processing in the background…":
    "インポートを開始しました。バックグラウンドで処理しています…",
  "{count} created": "{count} 件作成",
  "{count} updated": "{count} 件更新",
  "{count} datasets resolved": "{count} 件のデータセットを解決",
  "Import successful: {result}.{warnings}":
    "インポートに成功しました: {result}。{warnings}",
  " Warnings: {warnings}": " 警告: {warnings}",
  "Import failed: {message}": "インポートに失敗しました: {message}",
  "Failed to read file: {message}": "ファイルを読み取れませんでした: {message}",
  "Download failed: {message}": "ダウンロードに失敗しました: {message}",
  "Deleted {count} metrics.{notFound}":
    "{count} 件のメトリクスを削除しました。{notFound}",
  " {count} not found.": " {count} 件は見つかりませんでした。",
  "Bulk delete failed: {message}": "一括削除に失敗しました: {message}",
  "No metrics selected to export.":
    "エクスポートするメトリクスが選択されていません。",
  "No metrics match the current filter to export.":
    "現在のフィルターに一致するエクスポート対象のメトリクスはありません。",
  "Cancelled filtered {operation}: the metric list failed to load completely ({message}).":
    "フィルター対象の {operation} を中止しました。メトリクス一覧を完全に読み込めませんでした（{message}）。",
  "Import in progress": "インポート中",
  "Processing metrics: {processed}/{total} ({created} created, {updated} updated). This page will update automatically.":
    "メトリクスを処理中: {processed}/{total}（{created} 件作成、{updated} 件更新）。このページは自動的に更新されます。",
  "Import complete: {created} created, {updated} updated.":
    "インポート完了: {created} 件作成、{updated} 件更新。",
  "Import failed after processing {processed}/{total} metrics.":
    "{processed}/{total} 件のメトリクスを処理した後にインポートが失敗しました。",
  " Errors: {errors}": " エラー: {errors}",
  "Loading metrics": "メトリクスを読み込み中",
  "Governed business metrics with pre-compiled SQL expressions.":
    "事前コンパイル済み SQL 式を持つ、ガバナンス対象の業務メトリクスです。",
  "Delete {count} selected": "選択中の {count} 件を削除",
  "Export OSI": "OSI をエクスポート",
  "Filter metrics": "メトリクスをフィルター",
  "Find metrics": "メトリクスを検索",
  "Delete metrics": "メトリクスを削除",
  "Delete {count} selected metrics": "選択中の {count} 件のメトリクスを削除",
  "Delete all {count} metrics matching: {filter}":
    "一致する {count} 件のメトリクスをすべて削除: {filter}",
  "Permanently delete {count} selected metrics? This action cannot be undone.":
    "選択中の {count} 件のメトリクスを完全に削除しますか？この操作は元に戻せません。",
  "This action cannot be undone.": "この操作は元に戻せません。",
  "Export OSI metrics": "OSI メトリクスをエクスポート",
  Export: "エクスポート",
  "Export all {count} metrics in this namespace":
    "この名前空間の {count} 件のメトリクスをすべてエクスポート",
  "Export {count} selected metrics":
    "選択中の {count} 件のメトリクスをエクスポート",
  "Export all {count} metrics matching: {filter}":
    "一致する {count} 件のメトリクスをすべてエクスポート: {filter}",
  "Export filtered metrics (no filter applied)":
    "フィルター対象のメトリクスをエクスポート（フィルター未適用）",
  "Export failed": "エクスポート失敗",
  "Import OSI metrics": "OSI メトリクスをインポート",
  Import: "インポート",
  "OSI YAML file": "OSI YAML ファイル",
  "Select an OSI v1.0 YAML file containing metric definitions.":
    "メトリクス定義を含む OSI v1.0 YAML ファイルを選択してください。",
  "Accepted format: .yaml, .yml (OSI v1.0)":
    "対応形式: .yaml、.yml（OSI v1.0）",
  "Import failed": "インポート失敗",
  "External ID (add to the role's trust policy)":
    "外部 ID（ロールの信頼ポリシーに追加）",
  "Context Ontology Accelerator presents this value when assuming the role above. Add it as an sts:ExternalId condition on that role's trust policy — without the condition the role is assumable on behalf of any namespace in this deployment.":
    "Context Ontology Accelerator は上記ロールを引き受けるときにこの値を提示します。ロールの信頼ポリシーに sts:ExternalId 条件として追加してください。この条件がない場合、このデプロイのどの名前空間からでもロールを引き受けられます。",
  "Copy External ID": "外部 ID をコピー",
  Copy: "コピー",
  "External ID copied": "外部 ID をコピーしました",
  "Failed to copy External ID": "外部 ID をコピーできませんでした",
  "Loading…": "読み込み中…",
  "Glue database": "Glue データベース",
  "JDBC database": "JDBC データベース",
  "Databricks SQL Warehouse": "Databricks SQL Warehouse",
  Documents: "文書",
  "Athena (default)": "Athena（既定）",
  "Query Glue/Iceberg tables via Amazon Athena.":
    "Amazon Athena で Glue/Iceberg テーブルをクエリします。",
  "Redshift Serverless": "Redshift Serverless",
  "Query Glue/Iceberg tables via Redshift Serverless (awsdatacatalog auto-mount).":
    "Redshift Serverless（awsdatacatalog 自動マウント）で Glue/Iceberg テーブルをクエリします。",
  "Role ARN must match pattern: arn:aws:iam::ACCOUNT:role/{prefix}-datasource-access-*":
    "ロール ARN は arn:aws:iam::ACCOUNT:role/{prefix}-datasource-access-* の形式に一致する必要があります",
  "Must be a full Lambda function ARN: arn:aws:lambda:REGION:ACCOUNT:function:NAME":
    "完全な Lambda 関数 ARN を指定してください: arn:aws:lambda:REGION:ACCOUNT:function:NAME",
  "Name is required.": "名前は必須です。",
  "Catalog ID is required.": "カタログ ID は必須です。",
  "Database name is required.": "データベース名は必須です。",
  "Redshift workgroup is required when the execution engine is Redshift.":
    "実行エンジンが Redshift の場合、Redshift ワークグループは必須です。",
  "Select a database engine.": "データベースエンジンを選択してください。",
  "Host is required.": "ホストは必須です。",
  "Port is required.": "ポートは必須です。",
  "Secrets Manager ARN is required.": "Secrets Manager ARN は必須です。",
  "Connector function ARN is required.": "コネクタ関数 ARN は必須です。",
  "Select at least one file.": "1 つ以上のファイルを選択してください。",
  "Maximum {count} files.": "最大 {count} ファイルです。",
  "Unsupported: {files}": "未対応: {files}",
  "Bucket ARN is required.": "バケット ARN は必須です。",
  "Must be a valid S3 bucket ARN.":
    "有効な S3 バケット ARN を指定してください。",
  "S3 prefix must not be blank.":
    "S3 プレフィックスを空にすることはできません。",
  "S3 prefix must not start with a slash.":
    "S3 プレフィックスをスラッシュで始めることはできません。",
  'S3 prefix must not contain "..".':
    "S3 プレフィックスに「..」を含めることはできません。",
  'S3 prefix must not start with "s3://".':
    "S3 プレフィックスを「s3://」で始めることはできません。",
  "S3 prefix contains invalid characters.":
    "S3 プレフィックスに使用できない文字が含まれています。",
  "Fix the errors above.": "上記のエラーを修正してください。",
  "Upload failed. Please try again.":
    "アップロードに失敗しました。もう一度お試しください。",
  "Choose source type": "データソースの種類を選択",
  "Select the type of source you want to connect.":
    "接続するデータソースの種類を選択します。",
  "Source type": "データソースの種類",
  "Connect to an existing AWS Glue Data Catalog database. Covers S3/Iceberg tables, DynamoDB, and Athena-federated JDBC sources.":
    "既存の AWS Glue Data Catalog データベースに接続します。S3/Iceberg テーブル、DynamoDB、Athena フェデレーション JDBC ソースに対応します。",
  "Connect directly to a relational database. Schema is discovered from information_schema. Supported engines: {engines}.":
    "リレーショナルデータベースに直接接続します。information_schema からスキーマを検出します。対応エンジン: {engines}。",
  "Bring your own data source: register an Athena Query Federation SDK connector you built and deployed as a Lambda in your own AWS account. Schema and rows are read through Athena against that connector.":
    "独自データソースを接続します。自分の AWS アカウントに Lambda として構築・デプロイした Athena Query Federation SDK コネクタを登録し、Athena 経由でスキーマと行を読み取ります。",
  "Ingest unstructured documents from an S3 bucket or by uploading files directly.":
    "S3 バケットまたは直接アップロードから非構造化文書を取り込みます。",
  "Configure connection": "接続を設定",
  "Select the Glue Data Catalog database to connect.":
    "接続する Glue Data Catalog データベースを選択します。",
  "Source details": "データソースの詳細",
  "A unique name for this source within the namespace.":
    "名前空間内で一意のデータソース名です。",
  "My Glue Database": "My Glue Database",
  "Catalog ID (AWS account)": "カタログ ID（AWS アカウント）",
  Region: "リージョン",
  "Database name": "データベース名",
  "Choose a Glue Data Catalog database to connect. All tables within the database will be scanned.":
    "接続する Glue Data Catalog データベースを選択します。データベース内のすべてのテーブルがスキャンされます。",
  "Advanced configuration": "詳細設定",
  "Table filter": "テーブルフィルター",
  "Optional Glue expression (a regular expression, e.g. orders|customers) to include specific tables.":
    "対象テーブルを指定する任意の Glue 式（正規表現。例: orders|customers）です。",
  "Table exclude filter": "テーブル除外フィルター",
  "Execution engine": "実行エンジン",
  "How this Glue/Iceberg source's queries run at serve time. Redshift runs them via Redshift Serverless (awsdatacatalog auto-mount) instead of Athena. This does NOT connect to a Redshift database — to query a Redshift cluster's own tables, onboard a JDBC source instead.":
    "この Glue/Iceberg データソースのクエリ実行方式です。Redshift を選ぶと Athena の代わりに Redshift Serverless（awsdatacatalog 自動マウント）で実行します。Redshift データベースへの接続ではありません。Redshift クラスター内のテーブルをクエリする場合は JDBC データソースを登録してください。",
  "Redshift workgroup": "Redshift ワークグループ",
  "The Amazon Redshift Serverless workgroup that executes this source's queries.":
    "このデータソースのクエリを実行する Amazon Redshift Serverless ワークグループです。",
  "Athena DataCatalog name": "Athena DataCatalog 名",
  "Optional. Required for JDBC-backed Glue databases to enable federated queries.":
    "任意。JDBC バックエンドの Glue データベースでフェデレーションクエリを有効にする場合は必須です。",
  "Cross-account role ARN": "クロスアカウントロール ARN",
  "Provide connection details for your JDBC database.":
    "JDBC データベースの接続情報を入力します。",
  "My JDBC Database": "My JDBC Database",
  "Connection settings": "接続設定",
  "Database engine": "データベースエンジン",
  "Select an engine": "エンジンを選択",
  Host: "ホスト",
  Port: "ポート",
  "Credential secret ARN": "認証情報シークレット ARN",
  "AWS Secrets Manager secret containing the database username and password.":
    "データベースのユーザー名とパスワードを含む AWS Secrets Manager シークレットです。",
  "Schema filter": "スキーマフィルター",
  "Schema exclude filter": "スキーマ除外フィルター",
  "Optional glob(s) (* ? [seq]) to include specific schemas. Separate multiple with | or ,.":
    "対象スキーマを指定する任意の glob（* ? [seq]）です。複数指定する場合は | または , で区切ります。",
  "Optional glob(s) (* ? [seq]) to exclude specific schemas, in addition to the engine's system schemas (always excluded). Separate multiple with | or ,.":
    "除外するスキーマを指定する任意の glob（* ? [seq]）です。エンジンのシステムスキーマは常に除外され、それに加えて除外されます。複数指定する場合は | または , で区切ります。",
  "Optional glob(s) (* ? [seq]) to include specific tables. Separate multiple with | or ,.":
    "対象テーブルを指定する任意の glob（* ? [seq]）です。複数指定する場合は | または , で区切ります。",
  "Optional glob(s) (* ? [seq]) to exclude specific tables. Separate multiple with | or ,.":
    "除外するテーブルを指定する任意の glob（* ? [seq]）です。複数指定する場合は | または , で区切ります。",
  "Optional glob(s) (* ? [seq]) to exclude specific tables. Applied after the include filter. Separate multiple with | or ,.":
    "除外するテーブルを指定する任意の glob（* ? [seq]）です。対象フィルターの後に適用されます。複数指定する場合は | または , で区切ります。",
  "Entity classes and topics (optional)":
    "エンティティクラスとトピック（任意）",
  "Entity classes": "エンティティクラス",
  "What the extractor calls each thing it finds — a brand, a policy, a material. Each entity is recorded with its class, and those classes become classes in the ontology.":
    "抽出対象を表すクラス名です（ブランド、ポリシー、素材など）。各エンティティはクラスとともに記録され、そのクラスはオントロジーのクラスになります。",
  "Enter one at a time, or paste several separated by commas. {max} characters each; 15–25 classes works best.":
    "1件ずつ入力するか、カンマ区切りで複数貼り付けます。各 {max} 文字以内、15〜25クラスが推奨です。",
  "{count} of {max} classes": "{max} クラス中 {count} 件",
  "Infer from my documents": "文書から推論",
  "Recommended. Classes are derived from a sample of your documents at ingest.":
    "推奨。取り込み時に文書のサンプルからクラスを抽出します。",
  "Define my own": "自分で定義",
  "Your classes are preferred, and a new one is created when nothing on your list fits. No extra classes are derived from your documents.":
    "指定したクラスを優先し、該当するものがない場合は新しいクラスを作成します。文書から追加クラスは抽出しません。",
  "e.g. Loss Ratio, Claim Handler": "例: 損害率、保険金請求担当者",
  "Entity class labels": "エンティティクラスのラベル",
  Topics: "トピック",
  "What a passage is about, rather than what a thing is. Topics group related passages together and drive topic-based retrieval.":
    "対象物の種類ではなく、文章が何について述べているかを表します。関連する文章をまとめ、トピック検索に使用します。",
  "Enter one at a time, or paste several separated by commas. Phrases are fine, and your capitalisation is kept. {max} characters each.":
    "1件ずつ入力するか、カンマ区切りで複数貼り付けます。語句も使用でき、大文字・小文字は保持されます。各 {max} 文字以内です。",
  "{count} of {max} topics": "{max} トピック中 {count} 件",
  Automatic: "自動",
  "Recommended. Topics are extracted from your documents.":
    "推奨。文書からトピックを抽出します。",
  "Topics are still extracted from your documents; your names are used where one matches. Expect topics beyond your list.":
    "トピックは引き続き文書から抽出され、指定名と一致する場合にその名前を使用します。指定外のトピックも作成されます。",
  "e.g. Black Tie Gala, Quiet Luxury":
    "例: ブラックタイ・ガラ、クワイエット・ラグジュアリー",
  "Preferred topic names": "優先トピック名",
  "Enter a label.": "ラベルを入力してください。",
  "Labels must be {max} characters or fewer.":
    "ラベルは {max} 文字以内で入力してください。",
  "You can add at most {max} labels.":
    "ラベルは最大 {max} 個まで追加できます。",
  '"{label}" duplicates a label you have already added.':
    "「{label}」はすでに追加したラベルと重複しています。",
  "Enter a topic name.": "トピック名を入力してください。",
  "Topic names must be {max} characters or fewer.":
    "トピック名は {max} 文字以内で入力してください。",
  "You can add at most {max} topics.":
    "トピックは最大 {max} 個まで追加できます。",
  '"{topic}" duplicates a topic you have already added.':
    "「{topic}」はすでに追加したトピックと重複しています。",
  "{count} classes. Long lists measurably reduce how closely the extractor follows them.":
    "{count} クラスあります。長いリストでは抽出時の追従精度が低下します。",
  "“{typed}” as “{stored}”": "「{typed}」を「{stored}」として",
  "Extraction will record {items}.": "抽出時に {items} 記録します。",
  ", and {count} more": "、ほか {count} 件",
  "Extraction will record {items}{more}.":
    "抽出時に {items}{more} 記録します。",
  Warehouse: "ウェアハウス",
  "Snowflake virtual warehouse for discovery queries.":
    "検出クエリに使う Snowflake 仮想ウェアハウスです。",
  Role: "ロール",
  "Optional Snowflake RBAC role to assume.":
    "引き受ける任意の Snowflake RBAC ロールです。",
  "Point at the Athena Query Federation connector you deployed.":
    "デプロイ済みの Athena Query Federation コネクタを指定します。",
  Connector: "コネクタ",
  "Connector function ARN": "コネクタ関数 ARN",
  "The connector Lambda. It serves both metadata and record requests. It must live in the same AWS Region as Context Ontology Accelerator; a connector in another Region is rejected, because Athena can only invoke a data source connector co-located with the query.":
    "メタデータとレコードの両方のリクエストを処理するコネクタ Lambda です。Athena はクエリと同じリージョンにあるデータソースコネクタだけを呼び出せるため、Context Ontology Accelerator と同じ AWS リージョンに配置してください。",
  "The single database inside the connector's catalog that this source exposes. A connector serving several databases is onboarded once per database.":
    "このデータソースが公開するコネクタカタログ内の単一データベースです。複数データベースを提供するコネクタは、データベースごとに登録します。",
  "Configure source": "データソースを設定",
  "Upload files or connect an S3 bucket.":
    "ファイルをアップロードするか S3 バケットに接続します。",
  "Upload files": "ファイルをアップロード",
  Files: "ファイル",
  "PDF, DOCX, TXT, or Markdown. Maximum 100 files.":
    "PDF、DOCX、TXT、Markdown。最大 100 ファイル。",
  "Uploading files": "ファイルをアップロード中",
  "Uploading {count} file(s)…": "{count} ファイルをアップロード中…",
  "Triggering ingestion…": "取り込みを開始中…",
  "S3 bucket": "S3 バケット",
  "Source bucket ARN": "ソースバケット ARN",
  "S3 prefixes": "S3 プレフィックス",
  "Leave blank to ingest the entire bucket.":
    "バケット全体を取り込む場合は空欄にします。",
  "S3 prefix {number}": "S3 プレフィックス {number}",
  "Remove prefix": "プレフィックスを削除",
  "Add prefix": "プレフィックスを追加",
  "Cross-account access (optional)": "クロスアカウントアクセス（任意）",
  "Role ARN": "ロール ARN",
  "Advanced options": "詳細オプション",
  "Chunk size": "チャンクサイズ",
  "How much text goes into each unit that is extracted and embedded. A larger chunk keeps a full table row and its header together; raise it for CJK or dense, tabular documents.":
    "抽出と埋め込みを行う 1 単位に入れるテキストの量です。チャンクを大きくすると、表の 1 行とその見出しが同じチャンクに収まります。日本語などの CJK の文書や、表の多い文書では大きくしてください。",
  "Default (256 tokens)": "既定（256 トークン）",
  "Suits prose.": "文章が中心の文書に向いています。",
  "Large, for tabular or CJK text (1024 tokens)":
    "大（表や CJK のテキスト向け、1024 トークン）",
  "Keeps a full table row and its header in one chunk.":
    "表の 1 行とその見出しを 1 つのチャンクに収めます。",
  Custom: "カスタム",
  "Set the chunk size and overlap in tokens.":
    "チャンクサイズとオーバーラップをトークン数で指定します。",
  "Chunk size (tokens)": "チャンクサイズ（トークン）",
  "A whole number from 1 to {max}.": "1〜{max} の整数です。",
  "Chunk overlap (tokens)": "チャンクのオーバーラップ（トークン）",
  "Default (25 tokens)": "既定（25 トークン）",
  "Optional. Leave empty to use {overlap}. Must be smaller than the chunk size.":
    "任意です。空欄のときは {overlap} を使います。チャンクサイズより小さくしてください。",
  "Enter a whole number from 1 to {max}.":
    "1〜{max} の整数を入力してください。",
  "Enter a whole number from 1 to {max}, or leave it empty.":
    "1〜{max} の整数を入力するか、空欄にしてください。",
  "Must be larger than the overlap ({overlap} tokens).":
    "オーバーラップ（{overlap} トークン）より大きくしてください。",
  "Tracks document versions in Neptune.":
    "Neptune で文書のバージョンを追跡します。",
  "Enable versioning": "バージョン管理を有効化",
  "Deletes previous versions after re-ingestion.":
    "再取り込み後に以前のバージョンを削除します。",
  "Delete previous versions on re-ingestion":
    "再取り込み時に以前のバージョンを削除",
  "Configure enrichment": "エンリッチメントを設定",
  "Choose AI enrichment features.": "AI エンリッチメント機能を選択します。",
  "Metadata enrichment": "メタデータエンリッチメント",
  "When disabled, only technical metadata is captured during the scan. AI-generated descriptions, synonyms, glossary terms, and tags are skipped.":
    "無効にすると、スキャンでは技術メタデータだけを取得し、AI による説明、同義語、用語集の用語、タグの生成を省略します。",
  "Enable AI metadata enrichment": "AI メタデータエンリッチメントを有効化",
  "Auto-generate descriptions": "説明を自動生成",
  "Generate business-friendly descriptions for tables and columns":
    "テーブルと列に業務向けの説明を生成",
  "1–2 sentence business-friendly description per table and column. Tagged AI_GENERATED.":
    "テーブルと列ごとに 1〜2 文の業務向け説明を生成します。AI_GENERATED タグが付きます。",
  "Generate synonyms": "同義語を生成",
  "Generate business-language alternatives for tables and columns":
    "テーブルと列に業務用語の別名を生成",
  "2–5 business-language alternatives per table and column.":
    "テーブルと列ごとに 2〜5 件の業務用語の別名を生成します。",
  "Generate glossary terms": "用語集の用語を生成",
  "Map tables and columns to business domain concepts":
    "テーブルと列を業務ドメインの概念に対応付け",
  '1–3 business domain concepts (e.g., "Order Management", "Revenue").':
    "1〜3 件の業務ドメイン概念（例: 「注文管理」「売上」）。",
  "Constraint discovery": "制約を検出",
  "Enable AI-assisted cross-table relationship inference":
    "AI を利用したテーブル間関係の推論を有効化",
  "AI-inferred relationships are tagged as AI_INFERRED and require steward review before promotion.":
    "AI が推論した関係には AI_INFERRED タグが付き、昇格前にデータスチュワードのレビューが必要です。",
  "Review and connect": "確認して接続",
  "Review your configuration before connecting.":
    "接続前に設定内容を確認します。",
  Category: "カテゴリ",
  Connection: "接続",
  "Catalog ID": "カタログ ID",
  Engine: "エンジン",
  Authentication: "認証",
  Mode: "モード",
  "File upload": "ファイルアップロード",
  "Bucket ARN": "バケット ARN",
  "{count} file(s)": "{count} ファイル",
  "Enrichment settings": "エンリッチメント設定",
  Enabled: "有効",
  Disabled: "無効",
  "What happens next": "次の処理",
  "Context Ontology Accelerator will start the scan and enrichment pipeline. The data source status changes to Scanning while technical metadata is discovered, then to Enriching while AI generates business descriptions, synonyms, glossary terms, and tags. Once complete, the status changes to Pending review — you will be notified to review and approve the enriched metadata before it is used for ontology induction.":
    "Context Ontology Accelerator がスキャンとエンリッチメントのパイプラインを開始します。技術メタデータの検出中は「スキャン中」、AI が業務説明、同義語、用語集の用語、タグを生成している間は「エンリッチ中」になります。完了すると「レビュー待ち」になり、オントロジー誘導に使われる前に内容を確認して承認できます。",
  "Context Ontology Accelerator will scan the source and discover technical metadata only. AI metadata enrichment is disabled, so no business descriptions, synonyms, glossary terms, or tags will be generated. The status changes to Scanning, then directly to Pending review for steward approval.":
    "Context Ontology Accelerator はデータソースをスキャンして技術メタデータだけを検出します。AI メタデータエンリッチメントは無効なので、業務説明、同義語、用語集の用語、タグは生成されません。ステータスは「スキャン中」からデータスチュワード承認用の「レビュー待ち」に直接変わります。",
  "Upload failed": "アップロード失敗",
  "Failed to connect source": "データソースに接続できませんでした",
  "(none)": "（なし）",
  "Re-scan triggered.": "再スキャンを開始しました。",
  "Reject source": "データソースを却下",
  "Retry approve source": "データソースの承認を再試行",
  "Approve source": "データソースを承認",
  "Re-scan": "再スキャン",
  "Back to sources": "データソース一覧に戻る",
  "Failed to start re-scan": "再スキャンを開始できませんでした",
  "Bulk approval failed": "一括承認に失敗しました",
  "Bulk rejection failed": "一括却下に失敗しました",
  Processing: "処理中",
  Error: "エラー",
  "Scan completed, but metadata is incomplete":
    "スキャンは完了しましたが、メタデータが不完全です",
  "Metadata may be stale": "メタデータが古い可能性があります",
  "Re-scan ready for review": "再スキャンのレビュー準備ができました",
  "Source summary": "データソースの概要",
  "Tables discovered": "検出済みテーブル",
  "Tables approved": "承認済みテーブル",
  "Last scan": "最終スキャン",
  "Source ID": "データソース ID",
  "Tables ({count})": "テーブル（{count}）",
  Columns: "列",
  Enriched: "エンリッチ済み",
  "Review status": "レビューステータス",
  // Foreign-key / relationship review table (#1088). The review state itself
  // (PENDING_REVIEW / APPROVED / REJECTED) is a service enum rendered verbatim,
  // so only the column headings and the steward's two actions are translated.
  Review: "レビュー",
  Approve: "承認",
  "cross-source": "ソース間",
  "Foreign keys & relationships": "外部キーとリレーションシップ",
  New: "新規",
  "Pending deletion": "削除待ち",
  Keep: "保持",
  "Find tables": "テーブルを検索",
  "Filter tables": "テーブルをフィルター",
  "Reject selected": "選択項目を却下",
  "Approve selected": "選択項目を承認",
  "Tables will appear here once the scan completes.":
    "スキャンが完了すると、ここにテーブルが表示されます。",
  "Scan has not started yet.": "スキャンはまだ開始されていません。",
  "No tables discovered.": "テーブルが検出されませんでした。",
  "Scan history": "スキャン履歴",
  Auth: "認証",
  Versioning: "バージョン管理",
  "Delete previous versions": "以前のバージョンを削除",
  "Failed to delete source": "データソースを削除できませんでした",
  "Delete document source": "文書データソースを削除",
  "Delete data source": "データソースを削除",
  "Discard the open re-scan review?":
    "進行中の再スキャンレビューを破棄しますか？",
  "Discard and re-scan": "破棄して再スキャン",
  "Source catalog": "ソースカタログ",
  "3rd-party catalog": "サードパーティカタログ",
  "Steward-edited": "スチュワード編集",
  "Steward-specified": "スチュワード指定",
  "AI-generated": "AI 生成",
  "This description came from a non-AI source, but tags, synonyms, and glossary terms may still be AI-generated.":
    "この説明は AI 以外のソースから取得されていますが、タグ、同義語、用語集の用語は AI によって生成された可能性があります。",
  "AI-enriched hint": "AI エンリッチメントの補足",
  Added: "追加",
  Removed: "削除",
  Changed: "変更",
  "{count} table-level change": "テーブルレベルの変更 {count} 件",
  "{count} table-level changes": "テーブルレベルの変更 {count} 件",
  "{count} column change": "列の変更 {count} 件",
  "{count} column changes": "列の変更 {count} 件",
  "{tableCount}, {columnCount}": "{tableCount}、{columnCount}",
  "Changes since last approved scan": "前回の承認済みスキャンからの変更",
  "Table-level changes": "テーブルレベルの変更",
  "Column changes": "列の変更",
  "Column changes since last approved scan":
    "前回の承認済みスキャンからの列の変更",
  "New table": "新しいテーブル",
  "This table was discovered during the latest re-scan and did not exist in the last approved scan. There is no before/after to compare — review its generated metadata below and approve to add it.":
    "このテーブルは最新の再スキャンで検出され、前回の承認済みスキャンには存在しませんでした。比較対象となる以前の状態はありません。以下の生成済みメタデータを確認し、追加する場合は承認してください。",
  "Dropped table": "削除対象のテーブル",
  'This table was not found in the source during the latest re-scan. It will be deleted when you approve the re-scan — use "Keep table" above to retain it if the source removal was unintended (e.g. a partial scan or a changed filter).':
    "このテーブルは最新の再スキャンでデータソースに見つかりませんでした。再スキャンを承認すると削除されます。意図しない除外（部分スキャンやフィルター変更など）の場合は、上の「テーブルを保持」を使用してください。",
  'Keeping column "{name}". Approving the re-scan will no longer drop it.':
    "列「{name}」を保持します。再スキャンを承認しても削除されません。",
  "Keeping this table. Approving the re-scan will no longer delete it.":
    "このテーブルを保持します。再スキャンを承認しても削除されません。",
  Column: "列",
  References: "参照先",
  Confidence: "信頼度",
  "Data type": "データ型",
  Glossary: "用語集",
  "Glossary terms": "用語集の用語",
  Tags: "タグ",
  "Sample values": "サンプル値",
  Nullable: "NULL 許容",
  "Description source": "説明のソース",
  "No columns found.": "列が見つかりません。",
  "No columns match the filter.": "フィルターに一致する列がありません。",
  "Failed to load table": "テーブルを読み込めませんでした",
  "Approved {count} column.": "{count} 列を承認しました。",
  "Approved {count} columns.": "{count} 列を承認しました。",
  "Rejected {count} column.": "{count} 列を却下しました。",
  "Rejected {count} columns.": "{count} 列を却下しました。",
  "Approved {succeeded} of {total} column; {failed} failed.":
    "{total} 列中 {succeeded} 列を承認し、{failed} 列が失敗しました。",
  "Approved {succeeded} of {total} columns; {failed} failed.":
    "{total} 列中 {succeeded} 列を承認し、{failed} 列が失敗しました。",
  "Rejected {succeeded} of {total} column; {failed} failed.":
    "{total} 列中 {succeeded} 列を却下し、{failed} 列が失敗しました。",
  "Rejected {succeeded} of {total} columns; {failed} failed.":
    "{total} 列中 {succeeded} 列を却下し、{failed} 列が失敗しました。",
  "Table and all columns approved.": "テーブルとすべての列を承認しました。",
  "Table and all columns rejected.": "テーブルとすべての列を却下しました。",
  'Updated column "{name}".': "列「{name}」を更新しました。",
  "Table metadata updated.": "テーブルメタデータを更新しました。",
  "Keys & relationships updated.": "キーとリレーションシップを更新しました。",
  Back: "戻る",
  "Keep table": "テーブルを保持",
  "Reject table & all columns": "テーブルとすべての列を却下",
  "Approve table & all columns": "テーブルとすべての列を承認",
  "Table metadata": "テーブルメタデータ",
  "Table ID": "テーブル ID",
  Format: "形式",
  Location: "場所",
  "Edit keys": "キーを編集",
  "Keys & relationships": "キーとリレーションシップ",
  "Primary key": "主キー",
  "Foreign keys": "外部キー",
  "No foreign key relationships detected.":
    "外部キーのリレーションシップは検出されませんでした。",
  "{count} column pending review": "レビュー待ちの列が {count} 件あります",
  "{count} columns pending review": "レビュー待ちの列が {count} 件あります",
  "AI enrichment produced descriptions, synonyms, and glossary mappings for these columns. Review or edit them before approving.":
    "AI エンリッチメントによって、これらの列の説明、同義語、用語集の対応付けが生成されました。承認前に確認または編集してください。",
  "Find columns": "列を検索",
  "Filter columns": "列をフィルター",
  "Filter by review status": "レビューステータスでフィルター",
  "No columns discovered yet.": "列はまだ検出されていません。",
  "Column metadata": "列メタデータ",
  "Column selection": "列の選択",
  "{count} columns selected": "{count} 列を選択中",
  "Remove {value}": "{value} を削除",
  "Edit table: {name}": "テーブルを編集: {name}",
  "Edit column: {name}": "列を編集: {name}",
  "Edit keys: {name}": "キーを編集: {name}",
  "Press Enter to add.": "Enter キーで追加します。",
  "e.g. customers_table": "例: customers_table",
  "e.g. customer_id": "例: customer_id",
  "e.g. Customer": "例: Customer",
  "e.g. pii": "例: pii",
  "Save & approve": "保存して承認",
  "Failed to save keys": "キーを保存できませんでした",
  "Primary key columns": "主キー列",
  "Steward-specified keys override discovered ones.":
    "スチュワードが指定したキーは検出済みのキーより優先されます。",
  "Select primary key columns": "主キー列を選択",
  "Add foreign key": "外部キーを追加",
  "No foreign keys.": "外部キーはありません。",
  "Target table": "参照先テーブル",
  "Target column": "参照先列",
  "No tables found.": "テーブルが見つかりません。",
  "No tables match the filter.": "フィルターに一致するテーブルがありません。",
  "Failed to load source": "データソースを読み込めませんでした",
  "Bulk approve started. Status will update when complete.":
    "一括承認を開始しました。完了するとステータスが更新されます。",
  "Bulk reject started. Status will update when complete.":
    "一括却下を開始しました。完了するとステータスが更新されます。",
  "{count} table(s) approved.": "{count} 件のテーブルを承認しました。",
  "{count} table(s) rejected.": "{count} 件のテーブルを却下しました。",
  "{succeeded} of {total} table(s) approved. {failed} failed.":
    "{total} 件中 {succeeded} 件のテーブルを承認し、{failed} 件が失敗しました。",
  "{succeeded} of {total} table(s) rejected. {failed} failed.":
    "{total} 件中 {succeeded} 件のテーブルを却下し、{failed} 件が失敗しました。",
  'Keeping "{name}". Approving the re-scan will no longer delete it.':
    "「{name}」を保持します。再スキャンを承認しても削除されません。",
  "Discards this re-scan: restores changed tables to their last approved version, removes newly-added tables, and returns the source to Approved.":
    "この再スキャンを破棄します。変更されたテーブルを前回の承認済みバージョンに戻し、新しく追加されたテーブルを削除して、データソースを承認済みに戻します。",
  "Marks all pending tables and columns in this source as rejected. Already-approved or already-rejected items are not affected.":
    "このデータソースのレビュー待ちのテーブルと列をすべて却下します。承認済みまたは却下済みの項目には影響しません。",
  "Approves the re-scan's changes and deletes the tables and columns it found were removed from the source. Already-rejected items are not affected.":
    "再スキャンの変更を承認し、データソースから削除されたと判断されたテーブルと列を削除します。却下済みの項目には影響しません。",
  "Retries approving all pending tables and columns in this source. Already-rejected items are not affected.":
    "このデータソースのレビュー待ちのテーブルと列をすべて再度承認します。却下済みの項目には影響しません。",
  "Approves all pending tables and columns in this source, making them queryable. Already-rejected items are not affected.":
    "このデータソースのレビュー待ちのテーブルと列をすべて承認し、クエリ可能にします。却下済みの項目には影響しません。",
  'The background worker encountered an error. You can retry by clicking "Retry approve source".':
    "バックグラウンドワーカーでエラーが発生しました。「データソースの承認を再試行」をクリックして再試行できます。",
  'The background worker encountered an error. You can retry by clicking "Reject source".':
    "バックグラウンドワーカーでエラーが発生しました。「データソースを却下」をクリックして再試行できます。",
  "Bulk approval is in progress. This page will update automatically.":
    "一括承認を処理中です。このページは自動的に更新されます。",
  "Bulk rejection is in progress. This page will update automatically.":
    "一括却下を処理中です。このページは自動的に更新されます。",
  "The last scan succeeded but could not read {failedTables} it listed. Those tables have no columns, no comments and no declared keys below, and any AI-generated descriptions were produced over that gap — do not review this source as complete. Re-scan once the tables are readable.":
    "前回のスキャンは成功しましたが、一覧に含まれるテーブルのうち {failedTables} を読み込めませんでした。これらのテーブルには列、コメント、宣言済みキーが表示されず、AI による説明も欠落した情報を基に生成されています。このデータソースを完全なものとしてレビューせず、テーブルを読み取れる状態にしてから再スキャンしてください。",
  "1 of the tables": "1 件",
  "{count} of the tables": "{count} 件",
  "Affected tables ({shown} of {total} shown):":
    "影響を受けたテーブル（{total} 件中 {shown} 件を表示）:",
  "Affected tables:": "影響を受けたテーブル:",
  "This source was last scanned more than 30 days ago. The underlying schema may have changed since approval. Consider refreshing the source to ensure ontology induction uses up-to-date metadata.":
    "このデータソースの最終スキャンから 30 日以上経過しています。承認後に基盤スキーマが変更された可能性があります。最新のメタデータを使ってオントロジー誘導を行うため、データソースの再スキャンを検討してください。",
  "This source was re-scanned. New and changed tables and columns are marked Pending review — edit and approve them just like a first scan. Items the re-scan no longer found in the source are marked Pending deletion; approving the source deletes them. If a removal looks wrong (for example a partial scan or a changed include/exclude filter), use Keep to retain the table or column.":
    "このデータソースは再スキャンされました。新規または変更されたテーブルと列は「レビュー待ち」と表示されます。初回スキャンと同様に編集して承認してください。再スキャンで見つからなかった項目は「削除待ち」と表示され、データソースを承認すると削除されます。部分スキャンや対象フィルターの変更などで削除が不適切に見える場合は、「保持」を使ってテーブルまたは列を残してください。",
  "1 table could not be loaded due to a data issue. The list below may be incomplete.":
    "データの問題により 1 件のテーブルを読み込めませんでした。以下の一覧は不完全な可能性があります。",
  "{count} tables could not be loaded due to data issues. The list below may be incomplete.":
    "データの問題により {count} 件のテーブルを読み込めませんでした。以下の一覧は不完全な可能性があります。",
  "{count} column pending deletion": "削除待ちの列が {count} 件あります",
  "{count} columns pending deletion": "削除待ちの列が {count} 件あります",
  Overview: "概要",
  "S3 Prefixes": "S3 プレフィックス",
  "— (entire bucket)": "—（バケット全体）",
  "Source Bucket ARN": "ソースバケット ARN",
  "Cross-account Role": "クロスアカウントロール",
  "Last Updated": "最終更新",
  "Ingestion Statistics": "取り込み統計",
  "Total Files": "ファイル総数",
  Skipped: "スキップ",
  Errored: "エラー",
  "Documents Processed": "処理済みドキュメント",
  "Text Chunks AI Processed": "AI 処理済みテキストチャンク",
  "Text Chunks Embeddings": "埋め込み済みテキストチャンク",
  "Text Chunks Added To Graph": "グラフ追加済みテキストチャンク",
  "File Issues": "ファイルの問題",
  Filename: "ファイル名",
  Reason: "理由",
  "Permanently delete {name}? This will remove all associated files from S3, the DynamoDB record, and the knowledge graph data from Neptune and OpenSearch.":
    "{name} を完全に削除しますか？S3 の関連ファイル、DynamoDB レコード、Neptune と OpenSearch のナレッジグラフデータがすべて削除されます。",
  "Permanently delete {name}? This will remove all associated data.":
    "{name} を完全に削除しますか？関連データがすべて削除されます。",
  " has a re-scan waiting for your review. A new re-scan compares the database against the last version you approved, so anything you already approved, rejected, or edited inside this review is discarded.":
    " にはレビュー待ちの再スキャンがあります。新しい再スキャンはデータベースを前回承認したバージョンと比較するため、現在のレビューで承認、却下、編集した内容は破棄されます。",
  "Your approved tables and columns are not affected. To keep the decisions you have made in this review instead, cancel and finish reviewing it first.":
    "すでに承認済みのテーブルと列には影響しません。このレビューで行った判断を保持する場合は、キャンセルして先にレビューを完了してください。",
  "Error: {message}": "エラー: {message}",
  "The scan did not complete. Use Re-scan to try again.":
    "スキャンが完了しませんでした。「再スキャン」で再試行してください。",
  "The most recent scan did not complete. Use Re-scan to try again.":
    "直近のスキャンが完了しませんでした。「再スキャン」で再試行してください。",
  "Scan in progress": "スキャン処理中",
  "The scan or enrichment pipeline is still running.":
    "スキャンまたはエンリッチメントのパイプラインを実行中です。",
  "Scan cancelled": "スキャン中止",
  "The scan was cancelled.": "スキャンは中止されました。",
  "Scan completed": "スキャン完了",
  "Scanned {count} table.": "{count} 件のテーブルをスキャンしました。",
  "Scanned {count} tables.": "{count} 件のテーブルをスキャンしました。",
  "Re-scan approved": "再スキャンを承認",
  "Re-scan rejected": "再スキャンを却下",
  "Source approved": "データソースを承認",
  "Source rejected": "データソースを却下",
  "{count} table approved.": "{count} 件のテーブルを承認しました。",
  "{count} tables approved.": "{count} 件のテーブルを承認しました。",
  "Re-scan discarded; the previously approved tables were kept.":
    "再スキャンを破棄し、以前に承認されたテーブルを保持しました。",
  "Source rejected; no tables were published.":
    "データソースを却下し、テーブルは公開されませんでした。",
  "Source registered": "データソース登録",
  "Source created and queued for initial scan.":
    "データソースを作成し、初回スキャンのキューに追加しました。",
  "{scanned} {count} table could not be read and was skipped.":
    "{scanned} {count} 件のテーブルを読み取れなかったためスキップしました。",
  "{scanned} {count} tables could not be read and were skipped.":
    "{scanned} {count} 件のテーブルを読み取れなかったためスキップしました。",
  "All tables approved": "すべてのテーブルを承認",
  "{approved} of {discovered} tables approved.":
    "{discovered} 件中 {approved} 件のテーブルを承認しました。",
  "Deletion in progress": "削除処理中",
  "The source and all associated data are being removed.":
    "データソースと関連データを削除しています。",
  Time: "時刻",
  Details: "詳細",
  Activity: "アクティビティ",
  "No activity yet.": "アクティビティはまだありません。",
  "{count} tables": "{count} 件のテーブル",
  "Failed to load data sources": "データソースを読み込めませんでした",
  "Failed to load foundational ontologies":
    "基盤オントロジーを読み込めませんでした",
  "Induction failed": "誘導に失敗しました",
  "An induction is already in progress or a proposal is awaiting review for this namespace. Finish reviewing it before starting a new one.":
    "この名前空間では誘導を実行中か、提案がレビュー待ちです。新しい誘導を開始する前にレビューを完了してください。",
  "Induce an ontology from your approved sources, review the proposal, then accept it to publish.":
    "承認済みのデータソースからオントロジーを誘導し、提案をレビューして承認すると公開されます。",
  "Open Explorer": "エクスプローラーを開く",
  "An induction is already in progress or a proposal is awaiting review. Finish reviewing it before starting a new one.":
    "誘導を実行中か、提案がレビュー待ちです。新しい誘導を開始する前にレビューを完了してください。",
  "Start induction": "誘導を開始",
  "Induction in progress": "誘導を実行中",
  "You'll be redirected to the new proposal as soon as it's ready.":
    "新しい提案の準備ができ次第、自動的に移動します。",
  "Datasource already inducted": "データソースは誘導済みです",
  "This selection has the same structure as an existing accepted proposal, so no new proposal was created. Open the existing proposal to review or edit it.":
    "この選択内容は承認済みの既存提案と同じ構造のため、新しい提案は作成されませんでした。既存の提案を開いて確認または編集してください。",
  "View existing proposal": "既存の提案を表示",
  "Could not load data sources": "データソースを読み込めませんでした",
  "Induce Ontology": "オントロジーを誘導",
  Strategy: "戦略",
  "Induction strategies": "誘導戦略",
  "Table to Ontology — Maps each database table to an OWL class and columns to properties. Fast, deterministic, and cost-effective. Best for structured relational schemas where the table/column names are meaningful.":
    "テーブルからオントロジー — データベースの各テーブルを OWL クラスに、列をプロパティに対応付けます。高速かつ決定的で、コスト効率に優れます。テーブル名や列名に意味がある構造化リレーショナルスキーマに適しています。",
  "Unstructured (Lexical Graph) — Extracts concepts and relationships from document sources (PDFs, text) using NLP and builds a lexical knowledge graph. Exclusively for document sources. Supports grounding against loaded ontologies.":
    "非構造化（語彙グラフ）— NLP を使って文書データソース（PDF、テキスト）から概念と関係を抽出し、語彙ナレッジグラフを構築します。文書データソース専用で、読み込み済みオントロジーへのグラウンディングに対応します。",
  "Determines how the ontology is constructed from your data.":
    "データからオントロジーを構築する方法を指定します。",
  "Table to Ontology": "テーブルからオントロジー",
  "Unstructured (Lexical Graph)": "非構造化（語彙グラフ）",
  "Maps relational tables → OWL classes, columns → properties. Best for structured databases.":
    "リレーショナルテーブルを OWL クラス、列をプロパティに対応付けます。構造化データベースに適しています。",
  "Extracts concepts from document sources via NLP and builds a lexical knowledge graph. Exclusively for document sources.":
    "NLP を使って文書データソースから概念を抽出し、語彙ナレッジグラフを構築します。文書データソース専用です。",
  "Data sources": "データソース",
  "Select one or more document sources to induce from.":
    "誘導に使用する文書データソースを 1 つ以上選択します。",
  "Select one or more connected database sources to induce from.":
    "誘導に使用する接続済みデータベースソースを 1 つ以上選択します。",
  "Select data sources": "データソースを選択",
  "Grounding ontologies (optional)": "グラウンディング先オントロジー（任意）",
  "What is grounding?": "グラウンディングとは",
  "Grounding aligns your induced ontology against existing well-known ontologies. Instead of creating novel classes for every table or concept, the inducer checks if an equivalent concept already exists in a loaded ontology and reuses it.":
    "グラウンディングは、誘導したオントロジーを既存の広く知られたオントロジーに対応付けます。すべてのテーブルや概念に新しいクラスを作る代わりに、読み込み済みオントロジーに同等の概念があるか確認して再利用します。",
  "This improves interoperability — your ontology shares vocabulary with industry standards (Schema.org, FIBO, Dublin Core, etc.) making it easier to integrate with other systems.":
    "業界標準（Schema.org、FIBO、Dublin Core など）と語彙を共有できるため相互運用性が向上し、ほかのシステムと統合しやすくなります。",
  "This run grounds against EXACTLY the ontologies selected here. Your namespace's accepted induced ontologies are pre-selected by default (so new runs converge with prior ones), but you can deselect them. With nothing selected, every table/concept is treated as novel.":
    "この実行では、ここで選択したオントロジーだけをグラウンディング先にします。名前空間で承認済みの誘導オントロジーは既定で選択されますが、解除できます。何も選択しない場合、すべてのテーブルと概念を新規として扱います。",
  "Pick the ontologies to ground against for this run. Accepted induced ontologies are pre-selected (deselect to skip). Load foundational/uploaded ontologies from the Reference ontologies tab first.":
    "この実行のグラウンディング先を選択します。承認済みの誘導オントロジーは事前に選択されています。基盤またはアップロード済みオントロジーは、先に「参照オントロジー」タブから読み込んでください。",
  "Ontologies still embedding or failed to ingest are shown but not selectable.":
    "埋め込み処理中または取り込みに失敗したオントロジーは表示されますが、選択できません。",
  "Could not load ontologies: {error}":
    "オントロジーを読み込めませんでした: {error}",
  "Pick loaded ontologies to ground against":
    "グラウンディング先の読み込み済みオントロジーを選択",
  "⚠️ Ingest failed": "⚠️ 取り込み失敗",
  "⏳ Embedding…": "⏳ 埋め込み処理中…",
  Induced: "誘導",
  Foundational: "基盤",
  Uploaded: "アップロード済み",
  Unknown: "不明",
  "Grounding mode": "グラウンディングモード",
  "Grounding modes": "グラウンディングモード",
  "Enhanced — Three-stage matching: exact name match → embedding similarity recall → LLM disambiguation for ambiguous candidates. Most accurate but incurs ~1 LLM call per table. Use when precision matters and cost is acceptable.":
    "拡張 — 完全な名前一致、埋め込み類似度による候補抽出、LLM による曖昧な候補の判別という 3 段階で照合します。最も高精度ですが、テーブルごとに約 1 回の LLM 呼び出しが発生します。",
  "Standard — Two-stage matching: exact name match → embedding similarity with a confidence threshold. Fully deterministic, no LLM cost. Use for large schemas where speed and cost are priorities.":
    "標準 — 完全な名前一致と、信頼度しきい値を使った埋め込み類似度の 2 段階で照合します。完全に決定的で LLM コストは発生しません。",
  "How to match source concepts against the selected ontologies.":
    "データソースの概念を選択したオントロジーに照合する方法です。",
  "Enhanced (recommended)": "拡張（推奨）",
  "Exact-name match + embedding recall + LLM disambiguation. Best accuracy, ~1 LLM call per table.":
    "完全な名前一致、埋め込み候補抽出、LLM による判別を行います。最も高精度で、テーブルごとに約 1 回の LLM 呼び出しが発生します。",
  "Exact-name match + embedding recall only. Deterministic, faster, no LLM cost.":
    "完全な名前一致と埋め込み候補抽出だけを行います。決定的で高速、LLM コストは発生しません。",
  "Confidence threshold: {percent}%": "信頼度しきい値: {percent}%",
  "Only concepts above this threshold are auto-matched. Lower = more matches but more false positives.":
    "このしきい値を超える概念だけを自動照合します。下げると照合数は増えますが、誤検出も増えます。",
  Proposals: "提案",
  "Reference ontologies": "参照オントロジー",
  Ready: "準備完了",
  "Parse failed": "解析失敗",
  Ingesting: "取り込み中",
  accepted: "承認済み",
  "accept failed": "承認失敗",
  rejected: "却下済み",
  failed: "失敗",
  "Inducing…": "誘導中…",
  "Syncing embeddings…": "埋め込みを同期中…",
  accepting: "承認中",
  pending: "待機中",
  updated: "更新済み",
  cancelled: "キャンセル済み",
  "Failed to delete ontology: {error}":
    "オントロジーを削除できませんでした: {error}",
  "Failed to delete ontology": "オントロジーを削除できませんでした",
  '"{name}" uploaded. Parsing and embedding run in the background — class and embedding counts will appear here once ingestion completes. Refresh in a moment to check progress.':
    "「{name}」をアップロードしました。解析と埋め込みはバックグラウンドで実行され、取り込みが完了するとクラス数と埋め込み数が表示されます。しばらくしてから更新してください。",
  "{count} induced ontology in this namespace.":
    "この名前空間には誘導オントロジーが {count} 件あります。",
  "{count} induced ontologies in this namespace.":
    "この名前空間には誘導オントロジーが {count} 件あります。",
  "View in Explorer": "エクスプローラーで表示",
  "Ontology uploaded — ingestion in progress":
    "オントロジーをアップロードしました — 取り込み処理中",
  "Could not load ontologies": "オントロジーを読み込めませんでした",
  'Delete ontology "{name}"?': "オントロジー「{name}」を削除しますか？",
  'Type "delete" to confirm': "確認のため「delete」と入力してください",
  Title: "タイトル",
  Actions: "アクション",
  "Failed to load {name}: {error}": "{name} を読み込めませんでした: {error}",
  "unknown error": "不明なエラー",
  Load: "読み込む",
  "Delete in progress": "削除処理中",
  "Delete the induced ontology first — it's grounded against this reference, so this can't be removed while it exists. Open it from Explorer → Ontologies and use Delete ontology there.":
    "先に誘導オントロジーを削除してください。この参照オントロジーをグラウンディング先としているため、誘導オントロジーが存在する間は削除できません。エクスプローラーの「オントロジー」から開き、「オントロジーを削除」を使用してください。",
  "No induced ontologies yet. Run an induction and accept its proposal to see one here.":
    "誘導オントロジーはまだありません。誘導を実行して提案を承認すると、ここに表示されます。",
  "No foundational ontologies loaded yet. Load one from the catalog below.":
    "読み込み済みの基盤オントロジーはありません。以下のカタログから読み込んでください。",
  "No uploaded ontologies yet. Use Upload your own ontology above.":
    "アップロード済みのオントロジーはありません。上の「独自オントロジーをアップロード」を使用してください。",
  "No reference ontologies yet. Load one from the catalog, or upload your own.":
    "参照オントロジーはまだありません。カタログから読み込むか、独自のものをアップロードしてください。",
  "Ontologies available for grounding. Load one from the catalog, or upload your own.":
    "グラウンディングに利用できるオントロジーです。カタログから読み込むか、独自のものをアップロードしてください。",
  "Filter by ontology type": "オントロジー種別でフィルター",
  "All ({count})": "すべて（{count}）",
  "Uploaded ({count})": "アップロード済み（{count}）",
  "Foundational ({count})": "基盤（{count}）",
  "Upload your own ontology": "独自オントロジーをアップロード",
  "Reference Ontologies": "参照オントロジー",
  "Upload your own ontology file (Turtle, RDF/XML, or JSON-LD). For a curated public ontology (e.g. FIBO, Schema.org), use the Load action on its catalog row instead.":
    "独自のオントロジーファイル（Turtle、RDF/XML、JSON-LD）をアップロードします。FIBO や Schema.org などの公開オントロジーは、カタログ行の「読み込む」を使用してください。",
  "Ontology IRI": "オントロジー IRI",
  "Canonical IRI (e.g. http://example.org/myonto/). Used as the ontology_id.":
    "正規 IRI（例: http://example.org/myonto/）。ontology_id として使用されます。",
  "Display title": "表示タイトル",
  "My internal ontology": "社内オントロジー",
  File: "ファイル",
  "Turtle (.ttl), RDF/XML (.rdf), or JSON-LD (.jsonld).":
    "Turtle（.ttl）、RDF/XML（.rdf）、JSON-LD（.jsonld）。",
  "Drop ontology file here": "オントロジーファイルをここにドロップ",
  "Proposal ID": "提案 ID",
  Unstructured: "非構造化",
  Structured: "構造化",
  "{count} class": "{count} クラス",
  "{count} classes": "{count} クラス",
  "{count} table": "{count} テーブル",
  "No proposals yet.": "提案はまだありません。",
  "Failed to load graph": "グラフを読み込めませんでした",
  "Search failed": "検索に失敗しました",
  "Failed to load {name}": "{name} を読み込めませんでした",
  "Failed to open graph view": "グラフ表示を開けませんでした",
  Graph: "グラフ",
  "All ontologies": "すべてのオントロジー",
  "Failed to load ontology classes": "オントロジークラスを読み込めませんでした",
  "Failed to expand {name}": "{name} を展開できませんでした",
  View: "表示",
  List: "一覧",
  "Filter classes...": "クラスをフィルター...",
  "Search classes, properties...": "クラス、プロパティを検索...",
  "Restrict classes by ontology": "オントロジーでクラスを絞り込む",
  Search: "検索",
  "No results found. Try a different search term.":
    "結果が見つかりません。別の検索語を試してください。",
  "No classes found in this namespace yet.":
    "この名前空間にはクラスがまだありません。",
  "Showing {shown} of {total} root classes · double-click a class to expand its sub-classes":
    "{total} 件中 {shown} 件のルートクラスを表示 · クラスをダブルクリックするとサブクラスを展開します",
  "{nodes} nodes · {edges} edges · double-click a class to expand its sub-classes":
    "{nodes} ノード · {edges} エッジ · クラスをダブルクリックするとサブクラスを展開します",
  "{count} sub-class couldn't be loaded.":
    "{count} 件のサブクラスを読み込めませんでした。",
  "{count} sub-classes couldn't be loaded.":
    "{count} 件のサブクラスを読み込めませんでした。",
  Dismiss: "閉じる",
  "Loading classes…": "クラスを読み込み中…",
  Class: "クラス",
  "Couldn't load classes. See the error above and retry.":
    "クラスを読み込めませんでした。上のエラーを確認して再試行してください。",
  "No classes match your search.": "検索に一致するクラスがありません。",
  "Close detail panel": "詳細パネルを閉じる",
  Expand: "展開",
  "Graph view": "グラフ表示",
  "Some details couldn't be loaded.": "一部の詳細を読み込めませんでした。",
  Retry: "再試行",
  "Access denied": "アクセスが拒否されました",
  "Query resolution timed out": "質問の処理がタイムアウトしました",
  "Query resolution failed": "質問を処理できませんでした",
  "No result for this question": "この質問に対する結果はありませんでした",
  "The question could not be turned into a query":
    "質問をクエリに変換できませんでした",
  "A table in the question matches more than one source":
    "質問内のテーブルが複数のデータソースに一致します",
  "A data source is unavailable": "データソースを利用できません",
  "Received malformed response from server":
    "サーバーの応答の形式が正しくありません",
  "Your session has expired. Sign in again.":
    "セッションの有効期限が切れました。もう一度サインインしてください。",
  "The request failed": "リクエストに失敗しました",
  "Kind:": "種別:",
  "Description:": "説明:",
  "Synonyms:": "同義語:",
  "Defined in ontology:": "定義元オントロジー:",
  "Defined in ontologies:": "定義元オントロジー:",
  "Relationships ({count})": "リレーションシップ（{count}）",
  Predicate: "述語",
  Neighbor: "隣接要素",
  "Attributes ({count})": "属性（{count}）",
  Attribute: "属性",
  "Type / target": "型 / 参照先",
  "Matches ({count})": "一致（{count}）",
  "Near-duplicate / grounding links (skos:closeMatch / exactMatch) — drawn as edges in the graph; not counted as relationships.":
    "近似重複またはグラウンディングのリンク（skos:closeMatch / exactMatch）です。グラフではエッジとして描画され、リレーションシップ数には含まれません。",
  "Match type": "一致種別",
  "Browse the ontologies in this namespace, the classes inside them, and the sources they were induced from.":
    "この名前空間のオントロジー、そのクラス、誘導元データソースを参照します。",
  "Inducted sources": "誘導済みデータソース",
  "Could not load inducted sources":
    "誘導済みデータソースを読み込めませんでした",
  Datasource: "データソース",
  Label: "ラベル",
  "N/A": "該当なし",
  "{count} (novel)": "{count}（新規）",
  "No inducted sources yet. Run an induction to see the sources it used.":
    "誘導済みデータソースはまだありません。誘導を実行すると使用したデータソースが表示されます。",
  "Data sources that have fed an induction in this namespace.":
    "この名前空間の誘導に使用されたデータソースです。",
  Axioms: "公理",
  "No ontologies yet. Run an induction, or load a reference ontology from the Induction page.":
    "オントロジーはまだありません。誘導を実行するか、誘導ページから参照オントロジーを読み込んでください。",
  "No ontologies of this type.": "この種別のオントロジーはありません。",
  "Every ontology registered in this namespace. Open an induced ontology to browse its classes and relationships.":
    "この名前空間に登録されたすべてのオントロジーです。誘導オントロジーを開くと、クラスとリレーションシップを参照できます。",
  "Induced ({count})": "誘導（{count}）",
  "Failed to delete": "削除できませんでした",
  "Failed to load ontology source":
    "オントロジーのソースを読み込めませんでした",
  "Missing ontology": "オントロジーが指定されていません",
  "No ontology was specified.": "オントロジーが指定されていません。",
  "A delete is already in progress for this ontology.":
    "このオントロジーの削除処理はすでに進行中です。",
  "Download .ttl": ".ttl をダウンロード",
  "Delete ontology": "オントロジーを削除",
  "Induced Ontology": "誘導オントロジー",
  "Could not load graph contents": "グラフ内容を読み込めませんでした",
  "Showing a partial view": "一部のみ表示しています",
  "This ontology is larger than the page the tables load, so they show the first {classesShown} of {classesTotal} classes, {relationshipsShown} of {relationshipsTotal} relationships, and {attributesShown} of {attributesTotal} attributes. Download the .ttl for the complete ontology.":
    "このオントロジーは表で読み込めるページより大きいため、クラスは {classesTotal} 件中先頭 {classesShown} 件、リレーションシップは {relationshipsTotal} 件中 {relationshipsShown} 件、属性は {attributesTotal} 件中 {attributesShown} 件を表示しています。完全なオントロジーは .ttl をダウンロードしてください。",
  "Delete failed — retry to try again":
    "削除に失敗しました — 再試行してください",
  Relationships: "リレーションシップ",
  Attributes: "属性",
  "Grounded against": "グラウンディング先",
  "None (all classes novel)": "なし（すべて新規クラス）",
  "{grounded} of {total} classes grounded":
    "{total} クラス中 {grounded} クラスをグラウンディング",
  "Induced ontology graph contents": "誘導オントロジーのグラフ内容",
  "Classes ({count})": "クラス（{count}）",
  Origin: "由来",
  Grounded: "グラウンディング済み",
  Novel: "新規",
  "Grounded to": "グラウンディング先",
  "No classes persisted in the graph yet. If you just accepted a proposal, the graph may still be materializing — refresh in a moment.":
    "グラフに保存されたクラスはまだありません。提案を承認した直後はグラフを構築中の可能性があるため、しばらくしてから更新してください。",
  Relationship: "リレーションシップ",
  "Subject class": "主語クラス",
  "Object class": "目的語クラス",
  "No object-property relationships persisted in the graph.":
    "グラフに保存されたオブジェクトプロパティのリレーションシップはありません。",
  Datatype: "データ型",
  "No datatype-property attributes persisted in the graph.":
    "グラフに保存されたデータタイププロパティの属性はありません。",
  "Could not load source": "ソースを読み込めませんでした",
  "Loading source…": "ソースを読み込み中…",
  "No source available for this ontology.":
    "このオントロジーのソースはありません。",
  "Missing class": "クラスが指定されていません",
  "No class was specified.": "クラスが指定されていません。",
  "Could not load class": "クラスを読み込めませんでした",
  "Class relationships and attributes": "クラスのリレーションシップと属性",
  "This class has no object-property relationships.":
    "このクラスにはオブジェクトプロパティのリレーションシップがありません。",
  "This class has no datatype-property attributes.":
    "このクラスにはデータタイププロパティの属性がありません。",
  "Cedar authorizes each request on principal, action, and resource before any tier runs.":
    "Cedar は各層の実行前に、principal、action、resource に基づいてリクエストを認可します。",
  "The SQL Firewall rewrites compiled SQL with Cedar-derived row filters.":
    "SQL Firewall は、コンパイル済み SQL に Cedar 由来の行フィルターを適用します。",
  "Bedrock Guardrails filter prompts and outputs.":
    "Bedrock Guardrails はプロンプトと出力をフィルターします。",
  "Demo 1. Ask “total claims by region” for deterministic Tier 1, then “claims count” to reach the same metric through a synonym.":
    "デモ 1。「total claims by region」で決定的な Tier 1 を確認し、続けて「claims count」で同義語から同じメトリクスに到達します。",
  "Demo 2. Ask “show me all claims with amount greater than 10000” to see Tier 2 with compiled SPARQL and SQL.":
    "デモ 2。「show me all claims with amount greater than 10000」で、コンパイルされた SPARQL と SQL を使う Tier 2 を確認します。",
  "Demo 2 fallback. Ask “list all customers in northeast region”; when the Athena table is absent, the system falls through to Tier 3 without hiding the failure.":
    "デモ 2 のフォールバック。「list all customers in northeast region」を実行し、Athena テーブルが無い場合に失敗を隠さず Tier 3 へ移ることを確認します。",
  "Demo 3. Ask “what is the claims filing process” for a cited Tier 3 answer, then “explain underwriting guidelines” to see an honest low-confidence refusal.":
    "デモ 3。「what is the claims filing process」で引用付きの Tier 3 回答を確認し、続けて「explain underwriting guidelines」で低信頼度時の明示的な回答拒否を確認します。",
  "Demo 4. Choose Re-run as Tier 3 on a Tier 1 answer and compare the structurally different result.":
    "デモ 4。Tier 1 の回答で「Tier 3 として再実行」を選び、構造の異なる結果を比較します。",
  "Demo 5. Submit an empty query or a namespace with special characters to see fail-fast validation in under 100 ms.":
    "デモ 5。空のクエリまたは特殊文字を含む名前空間を送信し、100 ミリ秒未満の早期検証エラーを確認します。",
  "Demo 6. Copy the request-ID chip on any answer to follow it through the rationale drawer and CloudWatch.":
    "デモ 6。任意の回答にある request-ID チップをコピーし、根拠ドロワーと CloudWatch で追跡します。",
  "Back to class": "クラスに戻る",
  "Edit raw": "Raw データを編集",
  Apply: "適用",
  "Domain (class)": "ドメイン（クラス）",
  Range: "範囲",
  "Edit raw Turtle": "Turtle を直接編集",
  " ({type} match) ": "（{type} 一致）",
  "Exact vs close match": "完全一致と近似一致",
  "About exact vs close match": "完全一致と近似一致について",
  "Exact: the same concept as the foundational class (skos:exactMatch).":
    "完全一致: 基盤クラスと同一の概念です（skos:exactMatch）。",
  "Close: closely related but not identical (skos:closeMatch).":
    "近似一致: 密接に関連していますが同一ではありません（skos:closeMatch）。",
  "Ambiguous match to {name}": "{name} への曖昧な一致",
  " (low confidence, review Grounding tab)":
    "（信頼度が低いため、グラウンディングタブでレビューしてください）",
  "Primary key (owl:hasKey)": "主キー（owl:hasKey）",
  Inheritance: "継承",
  "PK-sharing": "主キー共有",
  " subclass detected from shared primary key":
    "（共有主キーからサブクラスを検出）",
  "Suggested parent": "提案された親クラス",
  "Suggested subclass": "提案されたサブクラス",
  " unconfirmed — review before accepting":
    "（未確認 — 承認前にレビューしてください）",
  Property: "プロパティ",
  "Target class": "対象クラス",
  "No relationships.": "リレーションシップはありません。",
  "No attributes.": "属性はありません。",
  "Grounding ({count})": "グラウンディング（{count}）",
  "No foundational grounding evaluated for this class.":
    "このクラスでは基盤オントロジーとのグラウンディングが評価されていません。",
  Match: "一致",
  "No sibling matches to other proposal classes.":
    "提案内の他のクラスとの一致はありません。",
  "Mapping: {table}": "マッピング: {table}",
  "Source column": "ソース列",
  "FK → {target}": "外部キー → {target}",
  "No column mappings parsed.": "解析できる列マッピングがありません。",
  "View raw R2RML": "Raw R2RML を表示",
  "Source (Turtle)": "ソース（Turtle）",
  " (unsaved — click Save changes to apply)":
    "（未保存 — 適用するには「変更を保存」をクリック）",
  " from {ontology}": "（{ontology} 由来）",
  " — exact name identity": " — 名前が完全に一致",
  " — low confidence, review before accepting":
    " — 信頼度が低いため、承認前にレビューしてください",
  "Kept novel (unsaved — click Save changes to apply)":
    "新規概念のまま保持（未保存 — 適用するには「変更を保存」をクリック）",
  "No confident match — {count} suggestion below":
    "確信度の高い一致はありません — 以下に {count} 件の候補があります",
  "No confident match — {count} suggestions below":
    "確信度の高い一致はありません — 以下に {count} 件の候補があります",
  "Novel concept — no foundational match":
    "新規概念 — 基盤オントロジーとの一致なし",
  "Reasoning: {reason}": "根拠: {reason}",
  "Scores below are embedding similarity (semantic closeness of the table description to each class definition). The match above was decided by name identity, not embedding score.":
    "以下のスコアは埋め込み類似度（テーブル説明と各クラス定義の意味的な近さ）です。上の一致は埋め込みスコアではなく、名前の同一性によって決定されました。",
  "Grounding candidate": "グラウンディング候補",
  "Ground to {name}": "{name} にグラウンディング",
  "Grounded to {name}": "{name} にグラウンディング済み",
  "Candidate class": "候補クラス",
  Score: "スコア",
  "Match confidence": "一致の信頼度",
  "About the match confidence score": "一致信頼度スコアについて",
  "How closely the table matches each candidate class, from 0 to 1 (higher is stronger).":
    "テーブルと各候補クラスの一致度を 0 から 1 で表します（高いほど強い一致です）。",
  "Enhanced mode blends embedding similarity with LLM confidence; Standard mode uses similarity alone.":
    "拡張モードは埋め込み類似度と LLM の信頼度を組み合わせ、標準モードは類似度のみを使用します。",
  "No foundational grounding candidates.":
    "基盤オントロジーのグラウンディング候補はありません。",
  "Clears grounding so this class becomes a new concept with no foundational link. Use when no candidate is a real match. Staged until you Save changes.":
    "グラウンディングを解除し、このクラスを基盤リンクのない新規概念にします。実際に一致する候補がない場合に使用します。「変更を保存」するまで仮保存されます。",
  "Mark novel (no grounding)": "新規概念にする（グラウンディングなし）",
  custom: "カスタム",
  required: "必須",
  unique: "一意",
  positive: "正数",
  reference: "参照",
  datatype: "データ型",
  "SHACL Constraints": "SHACL 制約",
  Beta: "ベータ",
  "SHACL constraints": "SHACL 制約",
  "About SHACL constraints": "SHACL 制約について",
  "Formal data-quality rules for this ontology (required fields, uniqueness, value ranges, references).":
    "このオントロジーに対する正式なデータ品質ルールです（必須項目、一意性、値の範囲、参照）。",
  "Context Ontology Accelerator pre-fills them from your database schema, and Infer adds domain rules using an AI model.":
    "Context Ontology Accelerator がデータベーススキーマから事前設定し、「制約を推論」で AI モデルを使ったドメインルールを追加します。",
  "Validate compiles the enabled rules and checks the proposal against them; violations are reported before you accept.":
    "「検証」は有効なルールをコンパイルして提案を確認し、承認前に違反を報告します。",
  "({count} active)": "（{count} 件有効）",
  "Uses an AI model to suggest domain rules the schema can't capture (value ranges, date ordering, allowed status values). Suggestions are tagged LLM to keep or remove.":
    "AI モデルを使い、スキーマだけでは表現できないドメインルール（値の範囲、日付の順序、許可されるステータス値）を提案します。提案には LLM タグが付き、保持または削除できます。",
  "Infer constraints": "制約を推論",
  "Compiles the enabled constraints into SHACL shapes and checks the proposal against them, reporting violations below. Also unlocks Export SHACL.":
    "有効な制約を SHACL シェイプへコンパイルし、提案を検証して違反を下に表示します。「SHACL をエクスポート」も有効になります。",
  Validate: "検証",
  "Downloads the compiled constraints as a standard SHACL Turtle file you can version, share, or run in any SHACL validator. Enabled after you run Validate.":
    "コンパイル済み制約を標準 SHACL Turtle ファイルとしてダウンロードします。バージョン管理、共有、任意の SHACL バリデーターでの実行が可能です。「検証」の実行後に有効になります。",
  "Export SHACL": "SHACL をエクスポート",
  "Inferring domain constraints from ontology...":
    "オントロジーからドメイン制約を推論しています...",
  "SHACL validation passed": "SHACL 検証に合格しました",
  "SHACL validation failed": "SHACL 検証に失敗しました",
  "No constraint config available. Run a new induction to auto-generate constraints from database schema.":
    "制約設定がありません。新しい誘導を実行すると、データベーススキーマから制約が自動生成されます。",
  "Per class: enable the rules to enforce.":
    "クラスごとに適用するルールを有効にします。",
  "Badges legend": "バッジの凡例",
  "About constraint sources and types": "制約のソースと種類について",
  "DB: from your database schema (NOT NULL, UNIQUE, keys, column types).":
    "DB: データベーススキーマ由来です（NOT NULL、UNIQUE、キー、列型）。",
  "LLM: a domain rule suggested by Infer. Review before trusting.":
    "LLM: 推論によって提案されたドメインルールです。使用前にレビューしてください。",
  "Custom: a rule you added (natural language or pasted SHACL).":
    "カスタム: 自然言語または貼り付けた SHACL として追加したルールです。",
  Types: "種類",
  "required: field must have a value. unique: at most one value per record. reference: must point to a related record. datatype: pins the value's type. positive: value above a minimum.":
    "必須: 項目には値が必要です。一意: レコードごとに値は最大 1 件です。参照: 関連レコードを指す必要があります。データ型: 値の型を固定します。正数: 最小値を超える必要があります。",
  "Enable constraint {name}": "制約 {name} を有効化",
  "Add custom rule": "カスタムルールを追加",
  'Natural-language rules are recorded for reviewer context but are not automatically compiled into SHACL. To enforce a rule, express it as a structured constraint above or paste SHACL in "Custom SHACL (advanced)".':
    "自然言語ルールはレビュー用の情報として記録されますが、SHACL には自動コンパイルされません。ルールを適用するには、上の構造化制約として表現するか、「カスタム SHACL（詳細）」に SHACL を貼り付けてください。",
  "Natural-language constraint rule": "自然言語の制約ルール",
  "e.g., Every policy must have a premium amount greater than zero":
    "例: すべての保険契約には 0 より大きい保険料が必要",
  "Custom SHACL (advanced)": "カスタム SHACL（詳細）",
  "Paste additional SHACL shapes in Turtle format. These are appended verbatim to the compiled constraints.":
    "追加の SHACL シェイプを Turtle 形式で貼り付けます。コンパイル済み制約へそのまま追加されます。",
  "Custom SHACL shapes (Turtle)": "カスタム SHACL シェイプ（Turtle）",
  "Proposed classes": "提案されたクラス",
  "({count} classes)": "（{count} クラス）",
  "Select a class to view its relationships, attributes, and mappings.":
    "クラスを選択すると、リレーションシップ、属性、マッピングを確認できます。",
  "Edit selected": "選択項目を編集",
  "About class origin": "クラスの生成元について",
  "Grounded: matched to a foundational ontology, reusing standard vocabulary.":
    "グラウンディング済み: 基盤オントロジーと一致し、標準語彙を再利用しています。",
  "Ambiguous: low-confidence match, review before accepting.":
    "曖昧: 信頼度の低い一致です。承認前にレビューしてください。",
  "Novel: created fresh, no confident match.":
    "新規: 確信度の高い一致がなく、新しく作成されました。",
  "Open a class and see its Grounding tab to change the match.":
    "クラスを開き、グラウンディングタブで一致先を変更できます。",
  Ambiguous: "曖昧",
  Matches: "一致",
  "No classes in this proposal.": "この提案にはクラスがありません。",
  "Prefixes (shared)": "プレフィックス（共有）",
  "Ungrouped properties": "未分類のプロパティ",
  "Properties whose domain class is not in this proposal.":
    "ドメインクラスがこの提案に含まれないプロパティです。",
  Domain: "ドメイン",
  Pending: "レビュー待ち",
  "Accepting…": "承認中…",
  Accepted: "承認済み",
  "Accept failed": "承認に失敗",
  Cancelled: "キャンセル済み",
  "Proposal updated": "更新済み",
  "Repair {issues} datatype issue across {names} datatype name":
    "{names} 件のデータ型名にある {issues} 件の問題を修復",
  "Repair {issues} datatype issue across {names} datatype names":
    "{names} 件のデータ型名にある {issues} 件の問題を修復",
  "Repair {issues} datatype issues across {names} datatype name":
    "{names} 件のデータ型名にある {issues} 件の問題を修復",
  "Repair {issues} datatype issues across {names} datatype names":
    "{names} 件のデータ型名にある {issues} 件の問題を修復",
  "Apply repair": "修復を適用",
  "These datatype names use non-standard spelling. Applying the repair rewrites them to the correct XSD datatype in your saved proposal. Your queries already work (the served copy is auto-corrected) — repairing keeps the saved ontology consistent with what gets served.":
    "これらのデータ型名には標準外の表記が使われています。修復を適用すると、保存済み提案内の正しい XSD データ型へ書き換えられます。クエリはすでに動作します（提供時のコピーでは自動修正されます）が、修復すると保存済みオントロジーと提供内容が一致します。",
  Change: "変更",
  Affected: "影響範囲",
  "non-standard capitalization": "標準外の大文字・小文字",
  "not a valid XSD datatype name": "有効な XSD データ型名ではありません",
  "{count} reference": "{count} 件の参照",
  "{count} references": "{count} 件の参照",
  "Validation {status}…": "検証 {status}…",
  "Running HermiT reasoner + structural metrics + OoPS! (if enabled)":
    "HermiT 推論エンジン、構造メトリクス、OoPS!（有効な場合）を実行しています",
  "Validation failed": "検証に失敗しました",
  "Validation passed": "検証に合格しました",
  "Validation failed: {count} error": "検証に失敗: {count} 件のエラー",
  "Validation failed: {count} errors": "検証に失敗: {count} 件のエラー",
  "{errors} error · {warnings} warning · {infos} info":
    "エラー {errors} 件 · 警告 {warnings} 件 · 情報 {infos} 件",
  "Object props": "オブジェクトプロパティ",
  "Datatype props": "データ型プロパティ",
  "Max depth": "最大深度",
  "Repair {count} datatype issue": "データ型の問題 {count} 件を修復",
  "Repair {count} datatype issues": "データ型の問題 {count} 件を修復",
  "Failed to parse ontology Turtle": "オントロジー Turtle の解析に失敗しました",
  "Could not apply this edit: the entry's original text was not found in the Turtle (it may have changed since you opened it). Close and re-open the entry, then try again.":
    "この編集を適用できませんでした。エントリの元のテキストが Turtle 内に見つかりません（開いた後に変更された可能性があります）。エントリを閉じて開き直し、再試行してください。",
  "Datatype repair failed: {error}": "データ型の修復に失敗しました: {error}",
  "Merged into ontology {id}": "オントロジー {id} にマージしました",
  "Accept failed. Re-accept to retry once the cause is resolved.":
    "承認に失敗しました。原因を解消してから再度承認してください。",
  "Accept is still in progress after 5 minutes. The ingest may complete in the background — refresh the page to recheck.":
    "5 分経過しても承認処理が進行中です。バックグラウンドで取り込みが完了する可能性があるため、ページを更新して再確認してください。",
  "Could not load proposal": "提案を読み込めませんでした",
  "Reviewing a proposal": "提案のレビュー",
  "About proposals": "提案について",
  "A proposal is a draft ontology generated from your data source. It is not live yet.":
    "提案はデータソースから生成されたオントロジーのドラフトです。まだ公開されていません。",
  "Review and edit the classes, properties, and mappings, then Accept to merge it into this namespace's ontology.":
    "クラス、プロパティ、マッピングを確認・編集し、「承認」でこの名前空間のオントロジーへマージします。",
  "Saves your edits (Turtle, R2RML, grounding choices) to the draft without accepting it.":
    "編集内容（Turtle、R2RML、グラウンディングの選択）を、承認せずにドラフトへ保存します。",
  "Read-only quality checks: reasoner consistency (HermiT), structural metrics, and design pitfalls (OoPS!, if enabled). Doesn't change the proposal.":
    "読み取り専用の品質チェックです。推論の整合性（HermiT）、構造メトリクス、設計上の問題（OoPS!、有効な場合）を確認します。提案は変更しません。",
  "Merges this draft into the namespace's live ontology (graph, search index, storage). Runs in the background; can't be undone here.":
    "このドラフトを名前空間の公開オントロジー（グラフ、検索インデックス、ストレージ）へマージします。バックグラウンドで実行され、ここでは元に戻せません。",
  "Already accepted": "承認済み",
  "Accept proposal": "提案を承認",
  "Proposal {id}…": "提案 {id}…",
  "Unsaved edits. Click Save changes to upload.":
    "未保存の編集があります。「変更を保存」をクリックしてアップロードしてください。",
  "Choose which edits to save": "保存する編集を選択してください",
  "Discard grounding choices": "グラウンディングの選択を破棄",
  "Discard Turtle edits": "Turtle の編集を破棄",
  "Grounding choices rebuild the ontology, so grounding and Turtle edits must be saved separately.":
    "グラウンディングの選択によってオントロジーが作り直されるため、グラウンディングと Turtle の編集は別々に保存する必要があります。",
  "Grounding overrides and Turtle edits must be saved separately":
    "グラウンディングの変更と Turtle の編集は別々に保存してください",
  "Could not save changes": "変更を保存できませんでした",
  "{message} Nothing was saved, so you can retry this save as-is.":
    "{message} 何も保存されていないため、このまま保存をやり直せます。",
  "The server refused this save: {message} Retrying unchanged will fail the same way.":
    "サーバーがこの保存を拒否しました: {message} 変更せずにやり直しても同じように失敗します。",
  "This save was rejected as malformed: {message}":
    "形式が正しくないため、この保存は拒否されました: {message}",
  "Failed to save proposal changes": "提案の変更を保存できませんでした",
  Failed: "失敗",
  "Previous accept attempt failed": "前回の承認処理に失敗しました",
  "Accepting proposal…": "提案を承認中…",
  "Waiting for embeddings to become searchable in the vector index.":
    "埋め込みがベクトルインデックスで検索可能になるのを待っています。",
  "Merging into ontology graph (Neptune + AOSS + S3 + embeddings).":
    "オントロジーグラフへマージしています（Neptune + AOSS + S3 + 埋め込み）。",
  "Save or discard your edits first": "先に編集を保存するか破棄してください",
  "Accept with unrepaired datatype issues?":
    "未修復のデータ型問題があるまま承認しますか？",
  "Accept anyway": "そのまま承認",
  "Repair & accept": "修復して承認",
  "This proposal has {count} datatype reference that uses non-standard spelling. If you accept without repairing, it is stored as-is in your ontology and any downloads (queries still work — the served copy is auto-corrected). Repairing first keeps your saved ontology consistent everywhere.":
    "この提案には標準外の表記を使うデータ型参照が {count} 件あります。修復せずに承認すると、オントロジーとダウンロード内容にはそのまま保存されます（クエリは動作し、提供時のコピーでは自動修正されます）。先に修復すると、保存済みオントロジーを一貫した状態に保てます。",
  "This proposal has {count} datatype references that use non-standard spelling. If you accept without repairing, they are stored as-is in your ontology and any downloads (queries still work — the served copy is auto-corrected). Repairing first keeps your saved ontology consistent everywhere.":
    "この提案には標準外の表記を使うデータ型参照が {count} 件あります。修復せずに承認すると、オントロジーとダウンロード内容にはそのまま保存されます（クエリは動作し、提供時のコピーでは自動修正されます）。先に修復すると、保存済みオントロジーを一貫した状態に保てます。",
  "Proposal status": "提案のステータス",
  "About proposal status": "提案のステータスについて",
  "Pending: ready to review and edit.":
    "レビュー待ち: レビューと編集ができます。",
  "Accepting / Syncing embeddings: merging into the graph and search index (a few minutes).":
    "承認中 / 埋め込み同期中: グラフと検索インデックスへマージしています（数分かかります）。",
  "Accepted: now part of the live ontology.":
    "承認済み: 公開オントロジーの一部になっています。",
  "Updated: edits saved, not yet accepted.":
    "更新済み: 編集は保存済みですが、まだ承認されていません。",
  "Rejected / Cancelled: the draft was discarded.":
    "却下済み / キャンセル済み: ドラフトは破棄されました。",
  "Unstructured Induction": "非構造化データの誘導",
  "Structured Induction": "構造化データの誘導",
  "Datasource(s)": "データソース",
  "Lexical Graph": "語彙グラフ",
  "Tables / columns": "テーブル / 列",
  "Novel classes": "新規クラス",
  "Novel vs grounded": "新規クラスとグラウンディング済みクラス",
  "About novel classes": "新規クラスについて",
  "New concepts created because they didn't match any grounding target.":
    "グラウンディング対象に一致しなかったため新しく作成された概念です。",
  "Classes that did match are reused (grounded) and not counted here. The graph colors grounded and novel classes differently.":
    "一致したクラスは再利用（グラウンディング）され、ここには含まれません。グラフではグラウンディング済みクラスと新規クラスを別の色で表示します。",
  "Loading ontology from storage…":
    "ストレージからオントロジーを読み込んでいます…",
  "Rendering ontology…": "オントロジーを描画しています…",
  "Ontology Turtle isn't valid": "オントロジー Turtle が無効です",
  "{error} The class list and graph won't update until the Turtle parses cleanly.":
    "{error} Turtle を正常に解析できるまで、クラス一覧とグラフは更新されません。",
  "Proposal graph": "提案グラフ",
  "Each cluster is a group of related classes; dashed purple arrows show subclass relationships. Click a class to inspect it, zoom in to read relationship names.":
    "クラスターは関連するクラスのまとまりで、紫色の破線矢印はサブクラス関係を表します。クラスをクリックすると詳細を確認でき、拡大するとリレーションシップ名が読めます。",
  "This proposal has {count} classes — too large to render as a graph (hidden above {maximum} for performance). Use the table and Turtle editor below to review and edit.":
    "この提案には {count} クラスあり、グラフとして描画するには大きすぎます（パフォーマンスのため {maximum} を超える場合は非表示）。下のテーブルと Turtle エディターでレビュー・編集してください。",
  "Rebuilding ontology with updated grounding...":
    "更新されたグラウンディングでオントロジーを再構築しています...",
  "R2RML Mapping": "R2RML マッピング",
  "R2RML mapping": "R2RML マッピング",
  "About R2RML mapping": "R2RML マッピングについて",
  "Maps each class and property back to its SQL table and columns, so questions about this ontology can be answered by querying the source database.":
    "各クラスとプロパティを元の SQL テーブルと列に対応付け、このオントロジーに関する質問をソースデータベースへのクエリで回答できるようにします。",
  "R2RML Turtle": "R2RML Turtle",
  "Ontology Source (Turtle)": "オントロジーソース（Turtle）",
  "Ontology Turtle": "オントロジー Turtle",
  "Constraint inference failed": "制約の推論に失敗しました",
  "Constraint inference timed out. Try again.":
    "制約の推論がタイムアウトしました。再試行してください。",
  "Edit: {name}": "編集: {name}",
  "Edit Turtle for {name}": "{name} の Turtle を編集",
  "Fetch metadata": "メタデータを取得",
  "Reads the source schema (tables, columns, types, keys). No row data.":
    "ソーススキーマ（テーブル、列、型、キー）を読み取ります。行データは読み取りません。",
  "Load grounding": "グラウンディングを読み込み",
  "Loads your chosen grounding ontologies for comparison. Skipped when no grounding is selected.":
    "選択したグラウンディング用オントロジーを比較のために読み込みます。選択がない場合はスキップされます。",
  Embeddings: "埋め込み",
  "Embeds each table and column to drive the similarity search in the next step.":
    "各テーブルと列を埋め込み、次のステップの類似検索に使用します。",
  "Match concepts": "概念を照合",
  "Matches concepts to the grounding ontologies (Enhanced mode uses an LLM; Standard uses similarity). Unmatched concepts become novel classes.":
    "概念をグラウンディング用オントロジーと照合します（拡張モードは LLM、標準モードは類似度を使用）。一致しない概念は新規クラスになります。",
  "Build OWL": "OWL を構築",
  "Builds the OWL ontology (a class per table, a property per column) plus R2RML mappings back to SQL.":
    "OWL オントロジー（テーブルごとにクラス、列ごとにプロパティ）と SQL へ戻る R2RML マッピングを構築します。",
  Persist: "保存",
  "Saves the ontology as a proposal. Nothing goes live until you accept it.":
    "オントロジーを提案として保存します。承認するまで公開されません。",
  Deterministic: "決定的",
  "AI-inferred": "AI 推論",
  "Auto-detected (PK-sharing)": "自動検出（主キー共有）",
  "Close class details": "クラス詳細を閉じる",
  "Subclass of": "次のサブクラス",
  Subclasses: "サブクラス",
  "Datatype properties": "データ型プロパティ",
  "Object properties": "オブジェクトプロパティ",
  "Inherited from {name}": "{name} から継承",
  "Remove this foundational ontology?": "この基盤オントロジーを削除しますか？",
  "Removing ": "",
  " deletes its graph triples and vector embeddings from this namespace, so it will no longer be available for grounding. It's a curated reference, so you can re-load it from the catalog afterward if needed.":
    " を削除すると、この名前空間からグラフトリプルとベクトル埋め込みが削除され、グラウンディングに使用できなくなります。これは管理された参照オントロジーのため、必要に応じて後からカタログから再読み込みできます。",
  "Deleting ": "",
  " permanently removes its classes, properties, graph triples, and vector embeddings from this namespace, so it can no longer ground future inductions and queries using its classes will stop returning results.":
    " を削除すると、そのクラス、プロパティ、グラフトリプル、ベクトル埋め込みがこの名前空間から完全に削除されます。今後の誘導でグラウンディングに使用できなくなり、そのクラスを使うクエリは結果を返さなくなります。",
  "It also deletes the proposals tied to it — both those that produced it and any grounded against it. This is generated content and cannot be recovered; re-creating it means re-running induction.":
    "このオントロジーを生成した提案と、このオントロジーに対してグラウンディングされた提案も削除されます。生成された内容は復元できず、再作成するには誘導を再実行する必要があります。",
  " permanently removes its graph triples, vector embeddings, and registry entry from this namespace. This is your own content and cannot be recovered.":
    " を削除すると、グラフトリプル、ベクトル埋め込み、レジストリエントリがこの名前空間から完全に削除されます。独自に追加した内容のため復元できません。",
  "{count} proposal awaiting review": "{count} 件の提案がレビュー待ちです",
  "{count} proposals awaiting review": "{count} 件の提案がレビュー待ちです",
  "Review proposal": "提案をレビュー",
  "New ontology changes have been proposed and are pending your review before they are added to the knowledge graph.":
    "オントロジーの変更が提案されています。ナレッジグラフへ追加する前にレビューしてください。",
  "Suggested inheritance": "提案された継承関係",
  "Looks like {child} could be a subclass of {parent}":
    "{child} は {parent} のサブクラスである可能性があります",
  "Auto-detected from PK-sharing — child and parent share the same primary-key column. Confirm to promote to rdfs:subClassOf, reject to leave the FK as a regular has-a relation.":
    "主キー共有から自動検出されました。子と親が同じ主キー列を共有しています。確定すると rdfs:subClassOf に昇格し、却下すると外部キーを通常の has-a 関係として残します。",
  Defer: "保留",
  "Found {count} similar pending suggestion on {parent}":
    "{parent} に対する類似の保留中提案が {count} 件見つかりました",
  "Found {count} similar pending suggestions on {parent}":
    "{parent} に対する類似の保留中提案が {count} 件見つかりました",
  "{children} share the same parent. Apply the same confirm to all?":
    "{children} は同じ親クラスを共有しています。すべて同様に確定しますか？",
  "Apply all {count}": "{count} 件すべてに適用",
  "Review individually": "個別にレビュー",
  "{instances} instances · {properties} properties":
    "インスタンス {instances} 件 · プロパティ {properties} 件",
  "Focus on this class": "このクラスにフォーカス",
  "Expand 1 hop": "1 ホップ展開",
  "Expand 2 hops": "2 ホップ展開",
  "Hide from graph": "グラフから非表示",
  "Edge labels": "エッジラベル",
  "Scroll to zoom · Drag to pan · Right-click for options":
    "スクロールでズーム · ドラッグで移動 · 右クリックでオプション",
  "{label} — double-click to expand sub-classes":
    "{label} — ダブルクリックでサブクラスを展開",
  "Novel class": "新規クラス",
  "Grounded class": "グラウンディング済みクラス",
  "Full screen": "全画面表示",
  "Exit full screen": "全画面表示を終了",
  "Control Panel": "操作パネル",
  "Zoom In": "拡大",
  "Zoom Out": "縮小",
  "Fit View": "全体を表示",
  "Mini Map": "ミニマップ",
  "Show {count} standalone class": "孤立したクラス {count} 件を表示",
  "Show {count} standalone classes": "孤立したクラス {count} 件を表示",
  Reject: "却下",
  Members: "メンバー",
  Roles: "ロール",
  "Failed to load members: {error}":
    "メンバーの読み込みに失敗しました: {error}",
  "No platform-level grants yet.":
    "プラットフォームレベルの権限はまだありません。",
  "Platform members": "プラットフォームメンバー",
  "{count} match": "{count} 件一致",
  "Are you sure you want to revoke platform access for {count} principal?":
    "{count} 件のプリンシパルのプラットフォームアクセスを取り消しますか？",
  "Are you sure you want to revoke platform access for {count} principals?":
    "{count} 件のプリンシパルのプラットフォームアクセスを取り消しますか？",
  "Platform roles": "プラットフォームロール",
  "Filter roles": "ロールを絞り込み",
  "Find roles": "ロールを検索",
  "No platform roles defined.": "プラットフォームロールが定義されていません。",
  "Name may only contain letters, numbers, hyphens, underscores, and dots":
    "名前には英数字、ハイフン、アンダースコア、ドットのみ使用できます",
  "Grant platform access": "プラットフォームアクセスを付与",
  "Platform role": "プラットフォームロール",
  "Select a platform role": "プラットフォームロールを選択",
  "No platform roles available": "利用可能なプラットフォームロールがありません",
  "Failed to load role details.": "ロール詳細の読み込みに失敗しました。",
  "Principal type": "プリンシパル種別",
  "Group / Team": "グループ / チーム",
  "Failed to load your roles": "ロールの読み込みに失敗しました",
  "Loading roles...": "ロールを読み込んでいます...",
  "Your roles": "あなたのロール",
  "Roles could not be loaded.": "ロールを読み込めませんでした。",
  "No roles assigned.": "割り当てられたロールはありません。",
  "Namespaces ({count})": "名前空間（{count}）",
  "Namespace memberships": "所属する名前空間",
  "No namespace memberships.": "所属する名前空間はありません。",
  "Role details": "ロール詳細",
  "Cedar policy": "Cedar ポリシー",
  Healthy: "正常",
  "Checking...": "確認中...",
  Degraded: "機能低下",
  "Failed to fetch system health": "システム状態の取得に失敗しました",
  "Real-time health status of all backend storage and AI services":
    "バックエンドのストレージと AI サービスの稼働状態をリアルタイムで表示します",
  "Last checked: {time}": "最終確認: {time}",
  "Overall Status": "全体の状態",
  "All systems operational": "すべてのシステムが正常に稼働しています",
  "Checking backend health...": "バックエンドの状態を確認しています...",
  "Unreachable: {error}": "到達できません: {error}",
  "One or more systems degraded": "1 つ以上のシステムで機能が低下しています",
  "Storage Backends": "ストレージバックエンド",
  "Sources and metadata table": "データソースとメタデータのテーブル",
  "GraphRAG knowledge graph (Gremlin/openCypher)":
    "GraphRAG ナレッジグラフ（Gremlin/openCypher）",
  "Vector embeddings collection (AOSS)": "ベクトル埋め込みコレクション（AOSS）",
  "Ontology Induction": "オントロジー誘導",
  "Ontology Graph Store": "オントロジーグラフストア",
  "Neptune DB — RDF/OWL ontology triples":
    "Neptune DB — RDF/OWL オントロジートリプル",
  "Ontology Vector Store": "オントロジーベクトルストア",
  "OpenSearch — ontology embeddings and semantic search":
    "OpenSearch — オントロジー埋め込みとセマンティック検索",
  "Bedrock Embeddings": "Bedrock 埋め込み",
  "Amazon Bedrock — text embedding model for vector generation":
    "Amazon Bedrock — ベクトル生成用のテキスト埋め込みモデル",
  "Description LLM": "説明生成 LLM",
  "Amazon Bedrock Converse — LLM for entity descriptions and induction":
    "Amazon Bedrock Converse — エンティティ説明と誘導用の LLM",
  "Trace detail": "トレース詳細",
  "Trace {requestId}": "トレース {requestId}",
  "Loading trace for request {id}...":
    "リクエスト {id} のトレースを読み込んでいます...",
  unknown: "不明",
  "Back to Playground": "プレイグラウンドに戻る",
  "Loading trace...": "トレースを読み込んでいます...",
  "Resolution summary": "解決結果の概要",
  Answer: "回答",
  "Total latency": "合計レイテンシー",
  "Tier outcome": "Tier の結果",
  "Tier {tier} — {label}": "Tier {tier} — {label}",
  metric: "メトリクス",
  ontology: "オントロジー",
  knowledge: "ナレッジ",
  "Request ID": "リクエスト ID",
  human: "ユーザー",
  Model: "モデル",
  "Boundary controls": "境界制御",
  "Cedar: {status}": "Cedar: {status}",
  "SQL Firewall: {status}": "SQL ファイアウォール: {status}",
  "Guardrails: {status}": "ガードレール: {status}",
  allow: "許可",
  ok: "正常",
  passed: "通過",
  blocked: "ブロック",
  "n/a": "該当なし",
  "Compiled artifacts": "コンパイル済みアーティファクト",
  "SPARQL & SQL": "SPARQL と SQL",
  "No compiled artifacts for this request.":
    "このリクエストにはコンパイル済みアーティファクトがありません。",
  "Every tier the request passed through, with the MCP tool invoked and the per-step latency.":
    "リクエストが通過した各 Tier、呼び出された MCP ツール、ステップごとのレイテンシーを表示します。",
  "Tier trace": "Tier トレース",
  Step: "ステップ",
  Tier: "Tier",
  "Tier {tier}": "Tier {tier}",
  Stage: "ステージ",
  Duration: "所要時間",
  Detail: "詳細",
  Resolved: "解決済み",
  Denied: "拒否",
  " (confidence {confidence})": "（信頼度 {confidence}）",
  " (truncated)": "（一部のみ表示）",
  " ({count} suppressed by SQL Firewall)":
    "（SQL ファイアウォールにより {count} 件抑制）",
  "Abandoned — {reason}.": "中止 — {reason}。",
  "Access authorized for {principal} (no row/column restrictions).":
    "{principal} のアクセスを認可しました（行・列の制限なし）。",
  "Access denied for principal {principal}.":
    "プリンシパル {principal} のアクセスを拒否しました。",
  "Ad Hoc Query": "アドホッククエリ",
  "Add table": "テーブルを追加",
  "Add value": "値を追加",
  "Add {label}": "{label}を追加",
  entries: "項目",
  classes: "クラス",
  topics: "トピック",
  tables: "テーブル",
  "Added {count} {items}.": "{items}を {count} 件追加しました。",
  "{count} skipped. {reasons}": "{count} 件をスキップしました。{reasons}",
  "{count} skipped. {reasons} And {remaining} other problem.":
    "{count} 件をスキップしました。{reasons} その他 {remaining} 件の問題があります。",
  "{count} skipped. {reasons} And {remaining} other problems.":
    "{count} 件をスキップしました。{reasons} その他 {remaining} 件の問題があります。",
  "Agentic Final Semantic Search": "エージェント最終セマンティック検索",
  "Agentic Retrieval Stop": "エージェント検索終了",
  "Agentic Session Start": "エージェントセッション開始",
  "Agentic Working Answer": "エージェント回答作成",
  "An unexpected error occurred while rendering this page.":
    "このページの表示中に予期しないエラーが発生しました。",
  "Applying guardrail": "ガードレールを適用中",
  "Ask a question against the governed surface...":
    "ガバナンスされた情報に質問してください...",
  "Assemble ontology context": "オントロジーコンテキストを構築",
  "Assemble response": "応答を構築",
  "Assembling response": "応答を構築中",
  "Authenticating with the configured Identity Provider...":
    "設定された ID プロバイダーで認証しています...",
  "Authentication failed. Redirecting...":
    "認証に失敗しました。リダイレクトしています...",
  "Authentication successful. Redirecting...":
    "認証に成功しました。リダイレクトしています...",
  Authorize: "認可",
  "Authorized.": "認可しました。",
  "Bedrock guardrail": "Bedrock ガードレール",
  "Breaking down the question": "質問を分解中",
  "Catalog Context": "カタログコンテキスト",
  "Catalog context": "カタログコンテキスト",
  "Catalog context loaded.": "カタログコンテキストを読み込みました。",
  "Cedar authorize": "Cedar 認可",
  "Checked metric catalog.": "メトリクスカタログを確認しました。",
  "Choosing the next tool": "次のツールを選択中",
  "Chose {strategy}.": "{strategy} を選択しました。",
  "Context Ontology Accelerator": "Context Ontology Accelerator",
  "Copy answer": "回答をコピー",
  Decompose: "分解",
  "Denied columns for row {row}": "{row} 行目の拒否対象列",
  "Dialect={dialect}": "方言={dialect}",
  "Discovering relationships": "関係を探索中",
  "Document chunks retrieved.": "文書チャンクを取得しました。",
  "Drafting an answer": "回答を作成中",
  Embed: "埋め込み",
  "Embed query": "クエリを埋め込み",
  "Embedded query ({count} dimensions).":
    "クエリを埋め込みました（{count} 次元）。",
  "Embedding failed: {error}": "埋め込みに失敗しました: {error}",
  "Embedding query": "クエリを埋め込み中",
  Empty: "空",
  Execute: "実行",
  "Execute SQL": "SQL を実行",
  "Execute SQL via VKG": "VKG 経由で SQL を実行",
  "Execute metric query": "メトリクスクエリを実行",
  "Execution plan": "実行計画",
  "Exploration Branch": "探索分岐",
  "Failed to render response": "応答を表示できませんでした",
  Field: "フィールド",
  "Final knowledge-base sweep": "ナレッジベースを最終検索中",
  "Finishing retrieval": "検索を終了中",
  Firewall: "ファイアウォール",
  "Focusing the graph traversal": "グラフ探索範囲を絞り込み中",
  "Following up a lead": "手掛かりを追跡中",
  "Generate SPARQL": "SPARQL を生成",
  "Generate SQL": "SQL を生成",
  "Generated {count} tokens.": "{count} トークンを生成しました。",
  "Generating SQL": "SQL を生成中",
  "Graph Traverse": "グラフ探索",
  "Graph context": "グラフコンテキスト",
  "Graph traversal": "グラフ探索",
  "Graph traversed.": "グラフを探索しました。",
  Guardrail: "ガードレール",
  "Intent routed.": "意図をルーティングしました。",
  "Knowledge base searched.": "ナレッジベースを検索しました。",
  Latency: "レイテンシー",
  "Lexical Baseline Retrieve": "字句ベースライン検索",
  "Lexical retrieval": "字句検索",
  "Lexical retrieval failed: {reason}.": "字句検索に失敗しました: {reason}。",
  "Llm Synthesize": "LLM 合成",
  "Loading ontology": "オントロジーを読み込み中",
  "Loading ontology context": "オントロジーコンテキストを読み込み中",
  "Match metric": "メトリクスを照合",
  'Matched {metric} but "{qualifier}" can\'t be applied to it; routing to structured query.':
    '{metric} に一致しましたが、"{qualifier}" を適用できないため構造化クエリへルーティングします。',
  "Metric Match": "メトリクス照合",
  "NL → SPARQL with confidence {confidence}.":
    "NL → SPARQL を信頼度 {confidence} で生成しました。",
  "NL→SQL skipped ({reason}); falling back to VKG.":
    "NL→SQL をスキップしました（{reason}）。VKG にフォールバックします。",
  "NL→SQL strategy abandoned.": "NL→SQL 戦略を中止しました。",
  "NL→SQL strategy failed": "NL→SQL 戦略に失敗",
  "Next Step Select": "次のステップを選択",
  "No Progress Step": "進展なしステップ",
  "No column restrictions. Add a table to deny specific columns.":
    "列の制限はありません。特定の列を拒否するテーブルを追加してください。",
  "No documents retrieved — the graph store timed out.":
    "文書を取得できませんでした — グラフストアがタイムアウトしました。",
  "No documents retrieved — {reason}.":
    "文書を取得できませんでした — {reason}。",
  "No matching metric; routing to structured query.":
    "一致するメトリクスがないため、構造化クエリへルーティングします。",
  "Ontology Edge Filter": "オントロジーエッジ絞り込み",
  "Ontology Resolved": "オントロジー解決",
  "Ontology context loaded.": "オントロジーコンテキストを読み込みました。",
  "Ontop could not translate SPARQL to SQL.":
    "Ontop は SPARQL を SQL に変換できませんでした。",
  "Ontop could not translate SPARQL to SQL: {reason}.":
    "Ontop は SPARQL を SQL に変換できませんでした: {reason}。",
  "Ontop could not translate the SPARQL to SQL":
    "Ontop は SPARQL を SQL に変換できませんでした",
  "Open full trace": "完全なトレースを開く",
  Other: "その他",
  "Parallel retrieval": "並列検索",
  "Parallel retrieval complete.": "並列検索が完了しました。",
  "Parallel retrieval.": "並列検索を実行しました。",
  "Parsed clean.": "正常に解析しました。",
  "Passed.": "通過しました。",
  "Planning approach": "進め方を計画中",
  "Present Edges Discovered": "既存エッジを検出",
  "Processing trace ({count} step)": "処理トレース（{count} ステップ）",
  "Processing trace ({count} steps)": "処理トレース（{count} ステップ）",
  Query: "クエリ",
  "Query embedded.": "クエリを埋め込みました。",
  "Query executed.": "クエリを実行しました。",
  "Question matched {count} metric, so it is ambiguous for the single-metric path; routing to structured query.":
    "質問が {count} 件のメトリクスに一致し、単一メトリクス経路では曖昧なため、構造化クエリへルーティングします。",
  "Question matched {count} metrics, so it is ambiguous for the single-metric path; routing to structured query.":
    "質問が {count} 件のメトリクスに一致し、単一メトリクス経路では曖昧なため、構造化クエリへルーティングします。",
  "Relevance: {score}%": "関連度: {score}%",
  "Reload page": "ページを再読み込み",
  Resolution: "解決方法",
  "Resolved to {metric} ({source}, confidence {confidence}).":
    "{metric} に解決しました（{source}、信頼度 {confidence}）。",
  "Resolving query": "クエリを解決中",
  "Resolving query · {step}": "クエリを解決中 · {step}",
  "Response Assemble": "応答構築",
  "Response synthesized.": "応答を合成しました。",
  Retried: "再試行済み",
  Retrieve: "検索",
  "Retrieved {count} document chunk(s) via {strategy}.":
    "{strategy} で文書チャンクを {count} 件取得しました。",
  "Retrieved {count} document chunk(s).":
    "文書チャンクを {count} 件取得しました。",
  "Retry SPARQL translation": "SPARQL 変換を再試行",
  "Retry attempted.": "再試行しました。",
  "Retry failed — {reason}.": "再試行に失敗しました — {reason}。",
  Retrying: "再試行中",
  "Retrying translation": "変換を再試行中",
  "Returned no rows with low confidence{confidence} — trying another approach.":
    "結果が 0 行で信頼度も低いため{confidence}、別の方法を試します。",
  "Route intent": "意図をルーティング",
  "Routing query": "クエリをルーティング中",
  "Run query": "クエリを実行",
  "Running a custom query": "カスタムクエリを実行中",
  "Running knowledge graph query": "ナレッジグラフクエリを実行中",
  "Running query": "クエリを実行中",
  "SPARQL generated.": "SPARQL を生成しました。",
  "SQL Firewall": "SQL ファイアウォール",
  "SQL firewall blocked the query — {reason}.":
    "SQL ファイアウォールがクエリをブロックしました — {reason}。",
  "SQL firewall blocked the query.":
    "SQL ファイアウォールがクエリをブロックしました。",
  "SQL firewall passed — query is a safe read-only SELECT.":
    "SQL ファイアウォールを通過しました — 安全な読み取り専用 SELECT です。",
  "SQL firewall passed.": "SQL ファイアウォールを通過しました。",
  "SQL generated ({details}).": "SQL を生成しました（{details}）。",
  "SQL generated.": "SQL を生成しました。",
  "Searching knowledge base": "ナレッジベースを検索中",
  "Searching knowledge sources": "ナレッジソースを検索中",
  "Searching metric catalog": "メトリクスカタログを検索中",
  Select: "選択",
  "Select a response to view its rationale.":
    "根拠を表示する応答を選択してください。",
  "Selecting applicable tools": "適用可能なツールを選択中",
  "Show less": "折りたたむ",
  "Show more": "さらに表示",
  "Show trace": "トレースを表示",
  "Showing {shown} of {total} rows": "{total} 行中 {shown} 行を表示",
  "Something went wrong": "問題が発生しました",
  "Sql Authorize": "SQL 認可",
  "Sql Execute": "SQL 実行",
  "Sql Failed": "SQL 失敗",
  "Sql Firewall": "SQL ファイアウォール",
  "Sql Generate": "SQL 生成",
  "Strategy Selected": "戦略選択",
  "Strategy abandoned; trying another approach.":
    "戦略を中止し、別の方法を試します。",
  "Strategy selected": "戦略を選択",
  "Strategy selected.": "戦略を選択しました。",
  "Subquestion Start": "サブクエスチョン開始",
  "Supporting content ({count})": "参考コンテンツ（{count} 件）",
  Synthesize: "合成",
  "Synthesize response": "応答を合成",
  "Synthesizing answer": "回答を合成中",
  "Table name for row {row}": "{row} 行目のテーブル名",
  "Tables: {tables}": "テーブル: {tables}",
  "The SPARQL the LLM emitted (Tier 2) and the SQL {engine} executed.":
    "LLM が生成した SPARQL（Tier 2）と、{engine}が実行した SQL。",
  "The SQL {engine} executed.": "{engine}が実行した SQL。",
  "Thinking...": "考えています...",
  "Tier 1 — Metric Match": "Tier 1 — メトリクス照合",
  "Tier 2 — Structured Query": "Tier 2 — 構造化クエリ",
  "Tier 2 — Structured Query ({strategies})":
    "Tier 2 — 構造化クエリ（{strategies}）",
  "Tier 2.5 — Structured Query": "Tier 2.5 — 構造化クエリ",
  "Tier 3 — Knowledge Retrieval": "Tier 3 — ナレッジ検索",
  "Timed out": "タイムアウト",
  "Tool: {tool}": "ツール: {tool}",
  "Tools Gated": "ツール選別",
  "Trace step: {step}": "トレースステップ: {step}",
  "Translate SPARQL → SQL": "SPARQL → SQL に変換",
  "Translated SPARQL to SQL.": "SPARQL を SQL に変換しました。",
  "Translated to SQL ({details}).": "SQL に変換しました（{details}）。",
  "Translating SPARQL to SQL": "SPARQL を SQL に変換中",
  "Translating to SPARQL": "SPARQL に変換中",
  "Traversing knowledge graph": "ナレッジグラフを探索中",
  "Try one of the sample prompts below, or type your own question.":
    "以下のサンプルプロンプトを選ぶか、質問を入力してください。",
  "Trying a different approach": "別の方法を試行中",
  "Unknown source": "不明なソース",
  "VKG strategy abandoned.": "VKG 戦略を中止しました。",
  "VKG strategy failed": "VKG 戦略に失敗",
  "Validate SPARQL": "SPARQL を検証",
  "Validating query": "クエリを検証中",
  "Validation error": "検証エラー",
  "Validation error: {error}": "検証エラー: {error}",
  "Vector Search": "ベクトル検索",
  "Vector search": "ベクトル検索",
  "Vkg Authorize": "VKG 認可",
  "Vkg Compile": "VKG コンパイル",
  "Vkg Context": "VKG コンテキスト",
  "Vkg Execute": "VKG 実行",
  "Vkg Failed": "VKG 失敗",
  "Vkg Firewall": "VKG ファイアウォール",
  "Vkg Retry": "VKG 再試行",
  "Vkg Translate": "VKG 変換",
  "Vkg Validate": "VKG 検証",
  Warning: "警告",
  "Working on a sub-question": "サブクエスチョンを処理中",
  "Zero compute consumed. Boundary checks: namespace regex, query length, character allowlist.":
    "コンピュートは消費されていません。境界チェック: 名前空間の正規表現、クエリ長、許可文字。",
  "a metric": "メトリクス",
  "assistant message": "アシスタントのメッセージ",
  "confidence {confidence}": "信頼度 {confidence}",
  "dialect {dialect}": "方言 {dialect}",
  exact: "完全一致",
  "firewall check failed": "ファイアウォールチェックに失敗しました",
  "generated SQL was not a safe SELECT":
    "生成された SQL は安全な SELECT ではありませんでした",
  "no matching tables found": "一致するテーブルが見つかりませんでした",
  "query execution failed": "クエリの実行に失敗しました",
  "tables: {tables}": "テーブル: {tables}",
  "the JDBC engine": "JDBC エンジン",
  "the generated SQL was not a safe read-only SELECT":
    "生成された SQL は安全な読み取り専用 SELECT ではありませんでした",
  "the graph store did not respond in time":
    "グラフストアが時間内に応答しませんでした",
  "the model returned no SQL": "モデルが SQL を返しませんでした",
  "the query engine": "クエリエンジン",
  "the request timed out": "リクエストがタイムアウトしました",
  "the search backend was unavailable":
    "検索バックエンドを利用できませんでした",
  "user message": "ユーザーのメッセージ",
  "{classes} classes, {properties} properties pulled from the published ontology.":
    "公開済みオントロジーからクラス {classes} 件、プロパティ {properties} 件を取得しました。",
  "{count} chunks retrieved.": "チャンクを {count} 件取得しました。",
  "{count} entities found.": "エンティティが {count} 件見つかりました。",
  "{count} items retrieved.": "項目を {count} 件取得しました。",
  "{count} items.": "{count} 件。",
  "{count} source cited": "{count} 件のソースを引用",
  "{count} sources cited": "{count} 件のソースを引用",
  "{count} sources in parallel": "{count} 件のソースを並列検索",
  "{count} sources in parallel.": "{count} 件のソースを並列検索しました。",
  "{duration} wall": "実時間 {duration}",
  "{duration}ms": "{duration}ms",
  "{duration}ms wall": "実時間 {duration}ms",
  "{input} → {output} tokens.": "{input} → {output} トークン。",
  "{items}, +{count} more": "{items}、ほか {count} 件",
  "{label}: {status}": "{label}: {status}",
  "{rows} row(s) returned from {tables}.":
    "{tables} から {rows} 行を返しました。",
  "{rows} row(s) returned.": "{rows} 行を返しました。",
  "~{duration} saved by running in parallel":
    "並列実行により約 {duration} 短縮",
  "More info": "詳細情報",
  "runtime-config.json must have region, authority, and clientId.":
    "runtime-config.json には region、authority、clientId が必要です。",
  "No runtime-config.json detected": "runtime-config.json が見つかりません。",
  "An unexpected error occurred.": "予期しないエラーが発生しました。",
  "Unavailable — the namespace could not be loaded. Reload the page to retrieve it.":
    "取得できません — 名前空間を読み込めませんでした。ページを再読み込みして取得してください。",
  "Unavailable — this namespace carries no External ID, so the role's trust policy cannot be bound to it. Report this to the operator of this deployment.":
    "取得できません — この名前空間には外部 ID がないため、ロールの信頼ポリシーを名前空間に紐付けられません。このデプロイの運用者に報告してください。",
  "this deployment's resource prefix followed by '{segment}'":
    "このデプロイのリソースプレフィックスに '{segment}' を続けた文字列",
  "Credential access role ARN is required, and the role's name must start with {prefix} — the assume grant is scoped to that prefix.":
    "認証情報アクセスロール ARN は必須です。ロール名は {prefix} で始める必要があります — AssumeRole の許可はこのプレフィックスに限定されています。",
  "Role name must start with '{prefix}'. This deployment's sts:AssumeRole grant is scoped to that prefix, so a role named anything else is denied however it is configured. The role's account is unconstrained.":
    "ロール名は '{prefix}' で始める必要があります。このデプロイの sts:AssumeRole の許可はこのプレフィックスに限定されているため、それ以外の名前のロールは設定にかかわらず拒否されます。ロールのアカウントに制約はありません。",
  "Workspace hostname is required.": "ワークスペースのホスト名は必須です。",
  "Must be a lowercase Databricks workspace host ending in cloud.databricks.com, azuredatabricks.net, or gcp.databricks.com.":
    "cloud.databricks.com、azuredatabricks.net、gcp.databricks.com のいずれかで終わる小文字の Databricks ワークスペースホストを指定してください。",
  "HTTP path is required.": "HTTP パスは必須です。",
  "Must be the warehouse's HTTP path: /sql/1.0/warehouses/ID (or /sql/1.0/endpoints/ID).":
    "ウェアハウスの HTTP パスを指定してください: /sql/1.0/warehouses/ID（または /sql/1.0/endpoints/ID）。",
  "Unity Catalog catalog is required.": "Unity Catalog のカタログは必須です。",
  "Must be a SQL identifier: a letter or underscore, then letters, digits, or underscores.":
    "SQL 識別子を指定してください: 先頭は英字またはアンダースコア、以降は英字、数字、アンダースコアです。",
  "Unity Catalog schema is required.": "Unity Catalog のスキーマは必須です。",
  "Credential secret ARN is required.": "認証情報シークレット ARN は必須です。",
  "Must be a Secrets Manager secret ARN: arn:aws:secretsmanager:REGION:ACCOUNT:secret:NAME":
    "Secrets Manager シークレット ARN を指定してください: arn:aws:secretsmanager:REGION:ACCOUNT:secret:NAME",
  "Connect one Unity Catalog schema on a Databricks SQL Warehouse. Nothing to deploy — Context Ontology Accelerator operates the connector; you supply the warehouse, the credential secret, and the role that reads it.":
    "Databricks SQL Warehouse 上の Unity Catalog スキーマを 1 つ接続します。デプロイは不要です — コネクタは Context Ontology Accelerator が運用し、ウェアハウス、認証情報シークレット、それを読み取るロールを指定します。",
  "Optional. For a Glue catalog in another account, the IAM role Context Ontology Accelerator assumes to read catalog metadata. Its name must start with {prefix}, and its trust policy must require the External ID below.":
    "任意。別アカウントの Glue カタログのメタデータを読み取るために Context Ontology Accelerator が引き受ける IAM ロールです。名前は {prefix} で始め、信頼ポリシーで下記の外部 ID を必須にしてください。",
  "Optional. For a credential secret in another account, the IAM role Context Ontology Accelerator assumes to read it. Its name must start with {prefix}, and its trust policy must require the External ID below.":
    "任意。別アカウントの認証情報シークレットを読み取るために Context Ontology Accelerator が引き受ける IAM ロールです。名前は {prefix} で始め、信頼ポリシーで下記の外部 ID を必須にしてください。",
  "Name the warehouse to read and the credential Context Ontology Accelerator may read it with.":
    "読み取るウェアハウスと、Context Ontology Accelerator がその読み取りに使う認証情報を指定します。",
  "My Databricks Warehouse": "My Databricks Warehouse",
  "SQL Warehouse": "SQL Warehouse",
  "Workspace hostname": "ワークスペースのホスト名",
  "The workspace's server hostname, without a scheme or path. AWS, Azure, and GCP workspaces are all accepted.":
    "ワークスペースのサーバーホスト名です。スキームやパスは含めません。AWS、Azure、GCP のワークスペースに対応します。",
  "A PrivateLink-only workspace is not reachable: the connector runs outside your VPC.":
    "PrivateLink 専用のワークスペースには接続できません。コネクタはお客様の VPC の外で動作します。",
  "HTTP path": "HTTP パス",
  "Copy it from the warehouse's Connection details tab. The Databricks analogue of a port — it selects which warehouse this source queries.":
    "ウェアハウスの「Connection details」タブからコピーします。Databricks でポートに相当する値で、このデータソースがクエリするウェアハウスを選びます。",
  "Keep the warehouse running, or make its auto-stop window match how often you expect questions: a stopped warehouse turns the first query into a resume, seconds on serverless and minutes on classic or pro.":
    "ウェアハウスを起動したままにするか、自動停止までの時間を質問の頻度に合わせてください。停止中のウェアハウスでは最初のクエリで再開が発生し、サーバーレスでは数秒、クラシックまたはプロでは数分かかります。",
  "Unity Catalog catalog": "Unity Catalog のカタログ",
  "Unity Catalog schema": "Unity Catalog のスキーマ",
  "The single schema this source exposes. A catalog with several schemas is onboarded once per schema.":
    "このデータソースが公開する 1 つのスキーマです。複数のスキーマを持つカタログは、スキーマごとに登録します。",
  "Every table must be SELECT-able by this credential. Visibility in information_schema comes from BROWSE or USE SCHEMA, so a table that is discovered is not necessarily one that can be read — and it would fail only when a question reaches it.":
    "すべてのテーブルがこの認証情報で SELECT できる必要があります。information_schema での可視性は BROWSE または USE SCHEMA で決まるため、検出されたテーブルが読み取れるとは限りません — 読み取れないテーブルは、質問がそのテーブルに到達したときに初めて失敗します。",
  "Credential access": "認証情報へのアクセス",
  'AWS Secrets Manager secret holding the Databricks credential, in this deployment\'s Region. Its JSON shape selects the authentication mode: {"token": "…"} for a personal access token, {"client_id": "…", "client_secret": "…"} for OAuth machine-to-machine.':
    'Databricks の認証情報を保持する、このデプロイのリージョンの AWS Secrets Manager シークレットです。JSON の形で認証方式が決まります: 個人用アクセストークンは {"token": "…"}、OAuth マシン間認証は {"client_id": "…", "client_secret": "…"} です。',
  "Machine-to-machine is the recommendation: a personal access token carries a user's identity and expires on a schedule you may not control.":
    "マシン間認証を推奨します。個人用アクセストークンはユーザーの ID に紐付き、管理できない周期で失効することがあります。",
  "Credential access role ARN": "認証情報アクセスロール ARN",
  "Required. The IAM role Context Ontology Accelerator assumes to read the secret above — it holds no Secrets Manager permission of its own, so there is no fallback. The role's name must start with {prefix}, and its trust policy must require the External ID below. Any AWS account, including this one.":
    "必須。上記のシークレットを読み取るために Context Ontology Accelerator が引き受ける IAM ロールです — Context Ontology Accelerator 自身は Secrets Manager の権限を持たないため、代替手段はありません。ロール名は {prefix} で始め、信頼ポリシーで下記の外部 ID を必須にしてください。このアカウントを含め、どの AWS アカウントのロールでも指定できます。",
  "Enrichment progress": "エンリッチメントの進捗",
  "{processed} of {total} tables processed":
    "{total} テーブル中 {processed} 件を処理済み",
  "Tables already reviewed count as processed without being regenerated.":
    "レビュー済みのテーブルは再生成せずに処理済みとして数えます。",
  "A filter had no effect on the last scan":
    "前回のスキャンで効果がなかったフィルターがあります",
  "Failed to load tables": "テーブルを読み込めませんでした",
  "The table list could not be retrieved.":
    "テーブル一覧を取得できませんでした。",
  "The list below is partial.": "以下の一覧は一部のみです。",
  "{count} relationship pending review":
    "レビュー待ちのリレーションシップ {count} 件",
  "{count} relationships pending review":
    "レビュー待ちのリレーションシップ {count} 件",
  "Still loading tables — the list below is incomplete.":
    "テーブルを読み込み中です — 以下の一覧はまだ不完全です。",
  "Credential access role (assumed to read the secret)":
    "認証情報アクセスロール（シークレットの読み取り時に引き受け）",
  "Credential secret (read through that role)":
    "認証情報シークレット（上記ロール経由で読み取り）",
  Trigger: "トリガー",
  "Automatically re-scan this source on a recurring cadence to pick up upstream schema changes. Unchanged tables keep their enrichment and review status.":
    "このデータソースを定期的に自動で再スキャンし、上流のスキーマ変更を取り込みます。変更のないテーブルはエンリッチメントとレビューの状態を保持します。",
  "Automatic rescans": "自動再スキャン",
  "Rescan schedule saved.": "再スキャンのスケジュールを保存しました。",
  "Enable scheduled rescans": "スケジュールによる再スキャンを有効にする",
  "Schedule expression": "スケジュール式",
  "EventBridge expression, e.g. rate(1 day) or cron(0 3 * * ? *).":
    "EventBridge の式です。例: rate(1 day)、cron(0 3 * * ? *)。",
  Timezone: "タイムゾーン",
  "IANA timezone for cron evaluation (e.g. America/New_York).":
    "cron の評価に使う IANA タイムゾーンです（例: America/New_York）。",
  "Save schedule": "スケジュールを保存",
  "Could not change event-driven rescans":
    "イベント駆動の再スキャンを変更できませんでした",
  "Event-driven rescans": "イベント駆動の再スキャン",
  "Re-scan automatically when this source's Glue Data Catalog changes (new/changed tables). Bursts are coalesced.":
    "このデータソースの Glue Data Catalog が変更されたとき（テーブルの追加・変更）に自動で再スキャンします。短時間に続いた変更はまとめて処理します。",
  "Re-scan on Glue catalog changes": "Glue カタログの変更時に再スキャン",
  "Press Enter to add. Pre-filled terms may be AI-generated; saving replaces the whole list.":
    "Enter キーで追加します。入力済みの語は AI 生成の場合があります。保存すると一覧全体が置き換わります。",
  "Clear all synonyms": "同義語をすべてクリア",
  "Clear all": "すべてクリア",
  "Graph evidence": "グラフの根拠",
  "A re-scanned source has an out-of-date induction":
    "再スキャンしたデータソースの誘導が古くなっています",
  "Re-scanned and re-approved since their last induction:":
    "前回の誘導以降に再スキャン・再承認されたデータソース:",
  ". That induction no longer matches the current source.":
    "。その誘導は現在のデータソースと一致しなくなっています。",
  "Right now, re-inducing only merges into the existing ontology (append-only): it won't remove tables dropped at the source and can duplicate edited labels. For a clean rebuild today, delete the affected induction below and induce again. Editing an induction in place isn't available yet.":
    "現時点では、再誘導は既存のオントロジーへの追記（append-only）だけを行います。データソースから削除されたテーブルは除去されず、編集済みのラベルが重複することがあります。きれいに作り直すには、下の該当する誘導を削除してから誘導し直してください。誘導をその場で編集する機能はまだありません。",
  "A pending induction predates a re-scan":
    "保留中の誘導は再スキャンより前のものです",
  "You have a pending induction for:": "保留中の誘導があるデータソース:",
  ". The source was re-scanned and re-approved after you triggered that induction.":
    "。このデータソースは、その誘導を開始した後に再スキャン・再承認されました。",
  "The pending proposal was built on the older source, so accepting it won't include the re-scan's changes. Re-run the induction on the current source, then accept the new proposal.":
    "保留中の提案は以前のデータソースに基づいているため、承認しても再スキャンの変更は含まれません。現在のデータソースで誘導をやり直し、新しい提案を承認してください。",
} as const;
