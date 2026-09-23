# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Japanese question scaffolding for the Tier-1 residual gate.

Entries must be tokens the script-run segmentation (``iter_word_tokens``) can
produce — one script per run — so a mixed kanji+kana phrase appears as its
split parts (``教えて`` tokenizes as ``教`` + ``えて``; both are listed because
that pair is the canonical polite ask). Longer all-hiragana fusions
(``えてください``, ``はいくらですか``) are handled by the hiragana
decomposition in ``metric_resolver._is_stop_scaffolding`` — a bounded DP that
splits an unspaced hiragana run into listed scaffolding words — not by listing
every combination here. An unlisted verb ("見せて" → ``見`` + ``せて``)
demotes to Tier 2, the safe direction, same contract as the English list.

Deliberately absent, per the package curation policy: aggregate modifiers
(平均/純 change WHICH aggregate is asked for) and time words (今日/昨日/先月/
きのう are time windows a fixed SQL template cannot express). Neither may ever
be added here. 合計/総/総額/件数 are safe for the same reason "total"/"count"
are: they restate the aggregate the metric's SQL already computes.
"""

from __future__ import annotations

GROUPS: tuple[str, ...] = (
    # Particles, copula, polite forms, and interrogative-quantity words:
    "は が を に で の と も か です ます だ ですか ますか でしょうか ください さい"
    " お ご 教 えて いくら どれくらい どのくらい",
    # Aggregate restaters (kanji runs), greetings, and units:
    "合計 総計 総額 総数 件数 金額 数 総"
    " こんにちは こんばんは おはよう ありがとう ありがとうございます よろしく どうも すみません"
    " 円 ドル ユーロ パーセント",
)
