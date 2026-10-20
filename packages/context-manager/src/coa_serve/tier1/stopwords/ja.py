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
every combination here. The DP never takes two one-scalar particles in a row
(that shape also spells content words such as ごご), so a grammatical pair such
as では must be listed in ``SAFE_PARTICLE_RUNS``. An unlisted verb ("見せて" → ``見`` + ``せて``)
demotes to Tier 2, the safe direction, same contract as the English list.

Deliberately absent, per the package curation policy: aggregate modifiers
(平均/純 change WHICH aggregate is asked for) and time words (今日/昨日/先月/
きのう are time windows a fixed SQL template cannot express). Neither may ever
be added here. 合計/総/総額/件数 are safe for the same reason "total"/"count"
are: they restate the aggregate the metric's SQL already computes.
"""

from __future__ import annotations

SAFE_PARTICLE_RUNS: frozenset[str] = frozenset(
    {
        # Reviewed grammatical fusions of two one-scalar particles. The
        # decomposition never takes two one-scalar steps in a row, because that
        # shape also spells content words (ごご, もも, かが, もの, ので), so each
        # pair that is pure grammar is listed here as a whole. Never add a pair
        # that is also a word: とも is left out because, in hiragana, it is also
        # a noun (友, 共) and a given name, so ともの<metric> is a qualifier.
        "では",
        "には",
        "とは",
        "のは",
        "でも",
        "にも",
        "での",
    }
)
"""Two-particle hiragana runs proven to be grammar, not content words.

``_is_stop_scaffolding`` accepts each entry on its own and also uses it as a
multi-scalar fragment inside a longer run (``総発電容量とはいくらですか``).
"""

GROUPS: tuple[str, ...] = (
    # Particles, copula, polite forms, and interrogative-quantity words.
    # Counter-bearing interrogatives are atomic kanji runs under the shared
    # tokenizer, so list each safe question form explicitly. The honorific
    # おいくら/おいくつ are listed whole: after a particle (はおいくら) the お
    # would be a second one-scalar step in a row, which the decomposition refuses.
    "は が を に で の と も か です ます だ ですか ますか でしょうか ください さい"
    " お ご 教 えて いくら いくつ おいくら おいくつ どれくらい どのくらい"
    " 何台 何件 何名 何社",
    # Aggregate restaters (kanji runs), greetings, and units:
    "合計 総計 総額 総数 件数 金額 数 総"
    " こんにちは こんばんは おはよう ありがとう ありがとうございます よろしく どうも すみません"
    " 円 ドル ユーロ パーセント",
)
