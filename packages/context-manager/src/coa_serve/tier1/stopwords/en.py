# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""English question scaffolding for the Tier-1 residual gate.

See the package docstring for the curation policy — in particular why
aggregate modifiers ("average", "net") and time words ("today", "current")
are deliberately absent from every group below.
"""

from __future__ import annotations

GROUPS: tuple[str, ...] = (
    # Interrogatives, copula, determiners, polite filler.
    "a an the what whats which who how is are was were be been being have has had do does did"
    " can could would will shall should give get show tell find report display list return fetch"
    " calculate compute me us my our ours we you your i it its please value much many"
    " overall just only there s very",
    # Aggregate/measure words a metric's own SQL already encodes. Only words that
    # describe the metric's EXISTING aggregate belong here — see the package
    # docstring for why "average"/"net" are deliberately absent.
    "total sum count number amount aggregate figure figures metric"
    " metrics kpi result results data stats statistic statistics",
    # Bare prepositions (their OBJECT is what survives as residual — see the
    # package docstring).
    "of for in on at to by",
    # Greetings and sign-offs. A chat UI wraps the question in conversational
    # padding ("Hello. What was total revenue? Thank you"); none of these asks
    # anything of the SQL, so omitting them let politeness alone trip the gate and
    # pointlessly demote a fully-consumed question to Tier 2 (review ask).
    #
    # Only words that cannot be anything BUT padding are listed. Note the absences:
    # "morning"/"afternoon"/"evening" are excluded despite "good morning", because
    # standalone they are time windows ("revenue this morning") — exactly the
    # qualifier class this gate exists to catch. A greeting that loses its second
    # word still gates; that is the safe direction to fail.
    "hello hi hiya hey greetings thanks thank thankyou thx cheers regards welcome",
    # Units and currencies — "revenue in USD" asks nothing extra of the SQL.
    "usd eur gbp jpy cad aud chf cny inr dollar dollars euro euros pound pounds yen currency percent percentage pct",
)
