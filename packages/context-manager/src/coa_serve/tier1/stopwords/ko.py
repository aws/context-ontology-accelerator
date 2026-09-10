# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Korean question scaffolding for the Tier-1 residual gate.

The Korean counterparts of the English groups: request verbs ("tell/show me"),
question words, counters that restate the metric's own aggregate (수/명/건),
and connective padding. Same closed-list philosophy: an unlisted harmless word
demotes to Tier 2 (slower, still answerable). Korean particles need no entries
of their own because they attach to the preceding word (tokens are
whitespace-delimited chunks).

Deliberately absent, per the package curation policy: aggregate modifiers
(평균, 누적) and time words (오늘, 어제, 지난주) — these are qualifiers a
metric's fixed SQL cannot express, so they must survive as residual.

Validated against a live deployment: 44 metrics x 89 call phrases x 6
condition-perturbing variants (534 questions) — 0 wrong-scope firings with
this list (356 with an ASCII-only tokenizer), and no correctly-routed
question changed tier across a 153-question corpus.
"""

from __future__ import annotations

GROUPS: tuple[str, ...] = (
    # Request verbs, question words, counters, connectives.
    "알려줘 알려줘요 알려주세요 알려주라 보여줘 보여줘요 보여주세요 구해줘 구해줘요 구해주세요"
    " 말해줘 말해주세요 해줘 해주세요 주세요 줘 좀 부탁해 부탁드립니다"
    " 얼마야 얼마 얼마나 얼마인지 몇 몇이야 몇인지 언제야 언제 언제인지 무엇 뭐야 뭐 뭔지"
    " 궁금해 궁금합니다 확인해줘 확인 조회해줘 조회 정리해줘 정리"
    " 수 수는 수와 수를 수가 명 명이야 건 개 값은 값이"
    " 그리고 및 랑 이랑 하고 같이 함께 기준 기준으로 대해 대한 관련",
)
