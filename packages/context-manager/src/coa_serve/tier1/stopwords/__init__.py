# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-language stop-word lists for the Tier-1 residual gate.

Each language contributes its *question scaffolding* — words that ask nothing
extra of a metric's SQL, so ignoring them can never change an answer. The gate
itself (mechanics, tokenizer) lives in ``metric_resolver``; this package only
curates the word lists, one module per language, merged below.

Curation policy (applies to every language)
-------------------------------------------

Only words whose removal cannot change the answer belong in a list. Filter-
bearing words must NOT be added: bare prepositions ("for"/"by"/"in") are safe
because the OBJECT they introduce ("for the Gold tier", "by region") is what
survives as residual and trips the gate. Two categories look like scaffolding
but are NOT, and are deliberately absent from every list:

* **Aggregate modifiers** — "average"/"avg"/"mean"/"net"/"gross" *change which
  aggregate is asked for*. A metric templating ``SUM(amount)`` answers
  "average revenue" with the SUM. "total"/"sum"/"count" are safe only because
  they restate the aggregate a metric already computes.
* **Time words** — "today"/"current"/"now"/"last week" are time windows a
  fixed SQL template cannot express. "Revenue today" is not the all-time
  total.

The rule of thumb: when unsure about a word, leave it out. The failure modes
are not symmetric — a missing stop word only demotes a question to Tier-2
(slower, still correct), while a wrongly added one makes the gate blind to a
real constraint and produces a confident wrong answer.

Adding a new language
---------------------

1. **Tokenizer first.** Add the script's character range to
   ``_RESIDUAL_TOKEN_RE`` in ``metric_resolver``; without it the gate cannot
   see the language at all. Caveat: the residual mechanism assumes
   whitespace-separated tokens. Korean qualifies (particles attach to the
   preceding word); Japanese, Chinese, or Thai would need word segmentation
   first, which is a larger change than a regex update.
2. **Curate the stop words** in a new module here (request verbs, question
   words, counters that restate the metric's own aggregate, connectives), and
   add it to the merge below.
3. **Keep qualifier-bearing words out** — the language's equivalents of
   *average*, *cumulative*, *net*, and all time words. Example of what goes
   wrong otherwise: with "last week" stop-listed, "revenue last week" fully
   matches a plain all-time "revenue" metric and returns the all-time number
   at confidence 1.0 with no warning.
4. **Validate against real data before shipping.** For Korean this meant
   simulating every call phrase x condition-perturbing variant (89 call
   phrases x 6 variants = 534 questions, run against the namespace's 44
   registered metrics) plus a 153-question corpus, and requiring both:
   perturbed variants demote to Tier-2 (misfires 356 -> 0), and zero
   correctly-routed questions change tier.
"""

from __future__ import annotations

from .en import GROUPS as _EN_GROUPS
from .ko import GROUPS as _KO_GROUPS

RESIDUAL_STOP_WORD_GROUPS: tuple[str, ...] = (*_EN_GROUPS, *_KO_GROUPS)
"""All languages' scaffolding groups, in a stable order (en, ko)."""

RESIDUAL_STOP_WORDS: frozenset[str] = frozenset(word for group in RESIDUAL_STOP_WORD_GROUPS for word in group.split())
"""The merged stop-word set the residual gate consumes."""
