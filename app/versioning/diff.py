"""Lightweight, human-readable diff between two versions of a section's text.

Word-level, not character-level: "40 mmHg" -> "30 mmHg" should read as one replaced word,
and table cells are compared as text. Numbers that appear or disappear are listed
separately because in this manual they are thresholds and limits (mmHg, seconds, %, codes).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.parsing.normalize import normalize_text

_NUMBER = re.compile(r"[±<>≥≤]?\d+(?:[.,]\d+)?(?:[–-]\d+(?:[.,]\d+)?)?%?|\bE\d+\b")


def numbers_in(text: str) -> list[str]:
    return _NUMBER.findall(normalize_text(text))


def word_diff(old: str, new: str, max_changes: int = 20) -> dict:
    a, b = normalize_text(old).split(), normalize_text(new).split()
    sm = SequenceMatcher(None, a, b, autojunk=False)
    changes = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        changes.append({
            "op": op,                                  # replace | delete | insert
            "old": " ".join(a[i1:i2]),
            "new": " ".join(b[j1:j2]),
            "context": " ".join(a[max(0, i1 - 4):i1]),  # a few preceding words
        })
    # Numbers are taken from the CHANGED spans, not by comparing the two texts as bags:
    # in 3.2 v2 "40" is replaced by "30" but v2 also says "the original 40 mmHg", so a
    # bag comparison would wrongly report that no number was removed.
    # Within one replacement, numbers on both sides cancel ("E1–E5" -> "E1–E6": only E5 -> E6).
    removed_all, added_all = [], []
    for c in changes:
        o, n = numbers_in(c["old"]), numbers_in(c["new"])
        removed_all += [x for x in o if x not in n]
        added_all += [x for x in n if x not in o]
    removed, added = _dedupe(removed_all), _dedupe(added_all)
    return {
        "similarity": round(sm.ratio(), 3),
        "words_old": len(a),
        "words_new": len(b),
        "changes": changes[:max_changes],
        "truncated": len(changes) > max_changes,
        "numbers_removed": removed,
        "numbers_added": added,
    }


def _dedupe(items) -> list[str]:
    seen: list[str] = []
    for x in items:
        if x not in seen:
            seen.append(x)
    return seen
