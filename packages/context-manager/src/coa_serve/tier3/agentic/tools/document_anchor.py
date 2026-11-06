# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Locate an anchor passage inside a full document (for anchored document reads).

The read_document tool can centre its window on a passage instead of starting at
offset 0. The anchor is either text the reasoning loop already saw — a retrieved
chunk, whose GraphRAG form is a JSON-ish ``{source, topic, statements}`` record
whose statements are EXTRACTED propositions rather than verbatim text — or a
short phrase / entity name from the question. :func:`choose_center` finds where
the anchor lives in the document with three increasingly tolerant matchers:

1. ``exact``      — a verbatim segment of the anchor occurs in the document;
2. ``normalized`` — the segment, or a run of consecutive distinctive words from
   it, occurs after case folding and collapsing punctuation/whitespace;
3. ``overlap``    — no literal match: the span whose distinctive words best
   overlap the anchor's (rarer words weigh more).

When several anchors (e.g. several chunks from one document) or several
occurrences are found, the chosen centre is the one whose window covers the most
anchors, then the most occurrences of the question's key terms.

Pure functions over strings; stdlib only (Requirement 12.3).
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass

# Words too common to identify a passage.
_STOPWORD_TEXT = (
    "a an and are as at be by for from has have in is it its of on or that the this to was "
    "were will with which who what when where how not no but if then than there their they "
    "them these those been being into about over under after before per each all any can may "
    "should would could also other such"
)
_STOPWORDS: frozenset[str] = frozenset(_STOPWORD_TEXT.split())

# Locating an anchor passage in a document:
# - segments shorter than _MIN_SEGMENT_CHARS match too many places to be useful;
# - _NGRAM_WORDS consecutive distinctive words identify a passage when the exact
#   text was rewritten (a summary statement rather than a quotation);
# - at most _MAX_POSITIONS_PER_ANCHOR candidate centres per anchor bound the work;
# - the overlap fallback scores spans of _OVERLAP_SPAN characters every
#   _OVERLAP_STRIDE characters (a paragraph or two, sampled finely enough that a
#   read window centred on the best span still covers the passage).
_MIN_SEGMENT_CHARS = 12
_NGRAM_WORDS = 5
_MAX_POSITIONS_PER_ANCHOR = 20
_OVERLAP_STRIDE = 400
_OVERLAP_SPAN = 2000

# JSON string values of at least 8 characters (shorter ones are keys or codes).
_STRING_RE = re.compile(r'"((?:[^"\\]|\\.){8,})"')
_WORD_RE = re.compile(r"[0-9a-z]+")


@dataclass(frozen=True)
class AnchorMatch:
    """Where an anchored window should be centred.

    Attributes:
        center: Character offset in the document to centre the window on.
        method: ``exact`` / ``normalized`` / ``overlap`` (the most precise matcher
            that located the winning anchor), or ``none`` when nothing matched.
        anchors_located: How many of the supplied anchors were located anywhere.
        anchors_in_window: How many located anchors fall inside the chosen window.
    """

    center: int
    method: str
    anchors_located: int = 0
    anchors_in_window: int = 0


def key_terms(text: str) -> list[str]:
    """Distinctive lowercase words of ``text`` (stopwords and 1-2 letter words dropped)."""
    out: list[str] = []
    for word in _WORD_RE.findall((text or "").lower()):
        if word in _STOPWORDS or (len(word) < 3 and not word.isdigit()):
            continue
        out.append(word)
    return out


def anchor_segments(anchor: str) -> list[str]:
    """Split an anchor into the passages worth searching for, longest first.

    A GraphRAG chunk contributes its ``topic`` and each ``statement`` (the source
    header is dropped — it names the file, not the content); plain text
    contributes itself and its lines/sentences.
    """
    text = (anchor or "").strip()
    if not text:
        return []
    segments: list[str] = []
    if text.startswith("{") and '"statements"' in text:
        after_source = re.sub(r'"source"\s*:\s*"(?:[^"\\]|\\.)*"', "", text, count=1)
        for raw in _STRING_RE.findall(after_source):
            try:
                value = json.loads(f'"{raw}"')
            except ValueError:
                value = raw
            if value not in ("topic", "statements"):
                segments.append(value.strip())
    else:
        segments.append(text)
        segments.extend(line.strip() for line in text.splitlines())
        segments.extend(part.strip() for part in re.split(r"(?<=[.!?])\s+", text))
    seen: set[str] = set()
    unique = []
    for seg in sorted(segments, key=len, reverse=True):
        if len(seg) >= _MIN_SEGMENT_CHARS and seg not in seen:
            seen.add(seg)
            unique.append(seg)
    if not unique and len(text) >= 3:
        unique.append(text)  # a short phrase / entity name
    return unique


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Lowercase ``text``, collapse non-alphanumeric runs to one space; map back to offsets."""
    out: list[str] = []
    index: list[int] = []
    pending_space = False
    for i, ch in enumerate(text):
        if ch.isalnum():
            if pending_space and out:
                out.append(" ")
                index.append(i)
            pending_space = False
            out.append(ch.lower())
            index.append(i)
        else:
            pending_space = True
    return "".join(out), index


def _normalize(text: str) -> str:
    return _normalize_with_map(text)[0]


def _all_positions(haystack: str, needle: str, limit: int = _MAX_POSITIONS_PER_ANCHOR) -> list[int]:
    out: list[int] = []
    start = haystack.find(needle)
    while start != -1 and len(out) < limit:
        out.append(start)
        start = haystack.find(needle, start + 1)
    return out


class _Document:
    """A document plus its normalized form and word statistics, built once per read."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.norm, self.index = _normalize_with_map(text)
        self.word_counts = Counter(key_terms(text))
        self._spans: list[tuple[int, set[str]]] | None = None

    def spans(self) -> list[tuple[int, set[str]]]:
        """(centre, distinctive words) for every overlap span, computed once per document."""
        if self._spans is None:
            self._spans = [
                (center, set(key_terms(self.text[max(0, center - _OVERLAP_SPAN // 2) :][:_OVERLAP_SPAN])))
                for center in range(0, len(self.text), _OVERLAP_STRIDE)
            ]
        return self._spans

    def to_original(self, norm_pos: int, length: int) -> int:
        """Original-text offset of the middle of a normalized match."""
        mid = min(len(self.index) - 1, norm_pos + length // 2)
        return self.index[mid] if self.index else 0

    def weight(self, word: str) -> float:
        """Rarer words identify a passage better (0 for words absent from the document)."""
        count = self.word_counts.get(word, 0)
        return 0.0 if count == 0 else 1.0 / math.log(2 + count)


def _locate(doc: _Document, anchor: str) -> tuple[list[int], str]:
    """Candidate centres for one anchor and the matcher that found them."""
    segments = anchor_segments(anchor)
    # 1. exact
    for seg in segments:
        positions = _all_positions(doc.text, seg)
        if positions:
            return [p + len(seg) // 2 for p in positions], "exact"
    # 2. normalized: whole segment, then runs of consecutive distinctive words
    for seg in segments:
        nseg = _normalize(seg)
        if len(nseg) < 3:
            continue
        positions = _all_positions(doc.norm, nseg)
        if positions:
            return [doc.to_original(p, len(nseg)) for p in positions], "normalized"
    hits: list[int] = []
    for seg in segments:
        words = _normalize(seg).split()
        for i in range(0, max(0, len(words) - _NGRAM_WORDS + 1)):
            gram = " ".join(words[i : i + _NGRAM_WORDS])
            if len(key_terms(gram)) < 3:
                continue  # mostly stopwords: not distinctive
            hits.extend(doc.to_original(p, len(gram)) for p in _all_positions(doc.norm, gram, limit=3))
        if len(hits) >= _MAX_POSITIONS_PER_ANCHOR:
            break
    if hits:
        return hits[:_MAX_POSITIONS_PER_ANCHOR], "normalized"
    # 3. overlap: the span whose distinctive words best overlap the anchor's
    terms = set(key_terms(" ".join(segments) or anchor))
    terms = {t for t in terms if doc.word_counts.get(t)}
    if not terms or not doc.text:
        return [], "none"
    best, best_score = 0, 0.0
    for center, span_terms in doc.spans():
        score = sum(doc.weight(t) for t in terms & span_terms)
        if score > best_score:
            best, best_score = center, score
    return ([best], "overlap") if best_score > 0 else ([], "none")


_METHOD_RANK = {"exact": 0, "normalized": 1, "overlap": 2, "none": 3}


def choose_center(text: str, anchors: list[str], question: str, window_chars: int) -> AnchorMatch:
    """Pick the window centre covering the most anchors, then the most question terms.

    Args:
        text: The full document text.
        anchors: Passages to locate (chunk texts and/or a phrase); empty ones ignored.
        question: The (sub-)question; its key terms break ties between candidates.
        window_chars: The window size the centre will be used with.

    Returns:
        An :class:`AnchorMatch`; ``method == "none"`` (centre 0) when no anchor
        could be located at all.
    """
    doc = _Document(text)
    located: list[tuple[list[int], str]] = []
    for anchor in anchors:
        if anchor and anchor.strip():
            positions, method = _locate(doc, anchor)
            if positions:
                located.append((positions, method))
    if not located:
        return AnchorMatch(center=0, method="none")

    half = max(1, window_chars // 2)
    qterms = set(key_terms(question))
    lowered = text.lower()

    def q_score(center: int) -> int:
        if not qterms:
            return 0
        window = lowered[max(0, center - half) : center + half]
        return sum(window.count(t) for t in qterms)

    best: tuple[int, int, int] | None = None  # (anchors covered, question terms, -method rank)
    best_center, best_method = 0, "none"
    for positions, method in located:
        for center in positions:
            covered = sum(1 for other, _ in located if any(abs(p - center) <= half for p in other))
            key = (covered, q_score(center), -_METHOD_RANK[method])
            if best is None or key > best:
                best, best_center, best_method = key, center, method
    return AnchorMatch(
        center=best_center,
        method=best_method,
        anchors_located=len(located),
        anchors_in_window=best[0] if best else 0,
    )
