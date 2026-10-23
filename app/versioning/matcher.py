"""Match sections of a new document version to sections of the previous version.

Strategy: layered, strictest evidence first. A pass only claims a match when exactly one
candidate qualifies on BOTH sides; otherwise the section falls through to the next pass.

    1. exact          same section number AND same (normalised) title
    2. title_parent   same title, and its parent is already matched to this node's parent
                      -> survives renumbering (e.g. 3.4/3.3 swapped back into order)
    3. number_title~  same number, similar title (>= TITLE_SIMILARITY) -> heading reworded
    4. same_body      identical non-empty content hash -> moved AND renamed, text untouched
    5. body~          body word-similarity >= BODY_SIMILARITY with a clear winner
    -  anything left: new side = "added", old side = "removed"

Design rule: when in doubt, DON'T match. A missed match shows up as removed + added,
which a human sees and a test-case owner re-checks. A wrong match silently ties old test
cases to a different section's text. In a regulated setting the second failure is worse.

Why not hash-first? Every chapter heading with no body text has the same hash (of ""),
so hashes cannot identify sections on their own. Why not path-only? Any renumbering would
turn an untouched section into "removed + added" and orphan its test cases.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable, Optional

from app.parsing.normalize import normalize_text

TITLE_SIMILARITY = 0.6    # "Cuff Inflation Sequence" vs "Cuff Inflation Procedure" = 0.72
BODY_SIMILARITY = 0.85    # word-level ratio; a reworded-but-same section scores well above
BODY_MIN_CHARS = 40       # bodies shorter than this are too generic to identify a section
CLEAR_WINNER_MARGIN = 0.05

ROOT_KEY = "doc"


@dataclass(frozen=True)
class NodeRef:
    """The minimum the matcher needs to know about a section (from the parser or the DB)."""

    key: str
    number: Optional[str]
    title: str
    parent_key: Optional[str]
    content_hash: str
    body: str


@dataclass
class Match:
    new_key: str
    old_key: Optional[str]          # None -> added
    method: str
    score: float
    status: str                     # added | unchanged | modified
    title_changed: bool = False
    moved: bool = False             # number or parent changed


@dataclass
class MatchResult:
    matches: dict[str, Match]                      # by new key
    removed: list[str]                             # old keys with no successor
    notes: list[str] = field(default_factory=list)  # ambiguities the matcher refused to resolve

    def by_status(self, status: str) -> list[str]:
        return [k for k, m in self.matches.items() if m.status == status]


def _title(n: NodeRef) -> str:
    return normalize_text(n.title).lower()


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _word_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a).split(), normalize_text(b).split()).ratio()


def match_versions(old: list[NodeRef], new: list[NodeRef]) -> MatchResult:
    old_by_key = {n.key: n for n in old}
    new_by_key = {n.key: n for n in new}
    pair: dict[str, tuple[str, str, float]] = {}   # new_key -> (old_key, method, score)
    taken_old: set[str] = set()
    notes: list[str] = []

    def free_new() -> list[NodeRef]:
        return [n for n in new if n.key not in pair]

    def free_old() -> list[NodeRef]:
        return [o for o in old if o.key not in taken_old]

    def claim(n: NodeRef, o: NodeRef, method: str, score: float) -> None:
        pair[n.key] = (o.key, method, score)
        taken_old.add(o.key)

    def unique_pass(method: str, key_fn: Callable[[NodeRef], Optional[tuple]]) -> None:
        """Match where the key value occurs exactly once among free old AND free new nodes."""
        olds, news = defaultdict(list), defaultdict(list)
        for o in free_old():
            k = key_fn(o)
            if k is not None:
                olds[k].append(o)
        for n in free_new():
            k = key_fn(n)
            if k is not None:
                news[k].append(n)
        for k, ns in news.items():
            os_ = olds.get(k, [])
            if len(ns) == 1 and len(os_) == 1:
                claim(ns[0], os_[0], method, 1.0)
            elif os_ and (len(ns) > 1 or len(os_) > 1):
                notes.append(
                    f"{method}: ambiguous {k!r} ({len(os_)} old vs {len(ns)} new candidates), not matched here"
                )

    # 0. roots always correspond
    if ROOT_KEY in old_by_key and ROOT_KEY in new_by_key:
        claim(new_by_key[ROOT_KEY], old_by_key[ROOT_KEY], "root", 1.0)

    # 1. exact number + title
    unique_pass("exact", lambda n: (n.number, _title(n)) if n.number else None)

    # 2. same title under the matched parent. Repeat: matching a parent can unlock children.
    while True:
        before = len(pair)

        def title_under_parent(n: NodeRef, side_new: bool) -> Optional[tuple]:
            if side_new:
                if n.parent_key not in pair:
                    return None
                return (_title(n), pair[n.parent_key][0])     # parent's OLD key
            return (_title(n), n.parent_key)

        olds, news = defaultdict(list), defaultdict(list)
        for o in free_old():
            olds[title_under_parent(o, False)].append(o)
        for n in free_new():
            k = title_under_parent(n, True)
            if k is not None:
                news[k].append(n)
        for k, ns in news.items():
            os_ = olds.get(k, [])
            if len(ns) == 1 and len(os_) == 1:
                claim(ns[0], os_[0], "title_parent", 1.0)
            elif os_ and len(ns) + len(os_) > 2:
                notes.append(f"title_parent: ambiguous title {k[0]!r} under {k[1]}, not matched here")
        if len(pair) == before:
            break

    # 3. same number, reworded title
    for n in free_new():
        if not n.number:
            continue
        cands = [o for o in free_old() if o.number == n.number]
        scored = [(o, _ratio(_title(n), _title(o))) for o in cands]
        scored = [s for s in scored if s[1] >= TITLE_SIMILARITY]
        if len(scored) == 1:
            claim(n, scored[0][0], "number_title~", round(scored[0][1], 3))

    # 4. identical, non-empty body (moved and renamed but text untouched)
    unique_pass("same_body", lambda n: n.content_hash if len(n.body.strip()) >= BODY_MIN_CHARS else None)

    # 5. similar body, clear winner only
    for n in free_new():
        if len(n.body.strip()) < BODY_MIN_CHARS:
            continue
        scored = sorted(
            ((o, _word_ratio(n.body, o.body)) for o in free_old() if len(o.body.strip()) >= BODY_MIN_CHARS),
            key=lambda s: -s[1],
        )
        if not scored or scored[0][1] < BODY_SIMILARITY:
            continue
        if len(scored) > 1 and scored[0][1] - scored[1][1] < CLEAR_WINNER_MARGIN:
            notes.append(f"body~: {n.key} is similar to several old sections, not matched")
            continue
        claim(n, scored[0][0], "body~", round(scored[0][1], 3))

    # ---- build result ------------------------------------------------------------------
    matches: dict[str, Match] = {}
    for n in new:
        if n.key not in pair:
            matches[n.key] = Match(n.key, None, "none", 0.0, "added")
            continue
        old_key, method, score = pair[n.key]
        o = old_by_key[old_key]
        # parent moved if the new parent does not correspond to the old parent
        new_parent_old = pair.get(n.parent_key, (None,))[0] if n.parent_key else None
        moved = n.number != o.number or new_parent_old != o.parent_key
        matches[n.key] = Match(
            new_key=n.key,
            old_key=old_key,
            method=method,
            score=score,
            status="unchanged" if n.content_hash == o.content_hash else "modified",
            title_changed=_title(n) != _title(o),
            moved=moved,
        )
    removed = [o.key for o in old if o.key not in taken_old]
    return MatchResult(matches=matches, removed=removed, notes=notes)
