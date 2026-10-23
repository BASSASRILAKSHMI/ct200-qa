"""Is a stored generation still valid against the CURRENT manual? Computed at read time.

Nothing about staleness is stored: every check runs against whatever version is latest right
now, so the answer cannot itself go stale when another version is ingested.

Three levels, cheapest and most certain first:

1. SOURCE level. For each source in the generation's snapshot, find the latest NodeVersion of
   the same logical section. Same content hash -> unchanged. Different -> changed (with a word
   diff). No row in the latest version -> removed.

2. QUOTE level, per test case. Every test case carries a verbatim `requirement_quote` (checked
   at generation time). If its cited sections changed, we check whether that quote still
   appears in their CURRENT text:
       quote gone      -> the requirement the test was written against changed  -> STALE
       quote still there -> the change is elsewhere in the section             -> REVIEW
   This is what separates "40 mmHg increments" (now 30, stale) from "initial target of
   180 mmHg" (same section, still true) in the real v1 -> v2 change.

3. SEVERITY. Numbers, limits and error codes are where a wrong test case hurts a patient, so:
       high   - a number/code inside the quote or the expected result was removed or
                changed by the edit (40 -> 30 mmHg, 2 -> 1.5 s, E5 -> E6), or a cited
                section was removed from the manual
       medium - the quoted wording changed, but no number the test depends on did
       low    - the quote is intact; something else in the section changed
       none   - nothing cited changed

Honest limits (also in docs/staleness.md):
* The quote is ONE anchor. A test can depend on a sentence it did not quote; a change there
  only produces REVIEW/low, not STALE.
* "Number changed" is token-based. "two seconds" -> "2 seconds" is a wording change to this
  check, and a moved decimal in an unrelated row of the same table can raise severity.
* A heading rename alone changes no hash, so it is reported as unchanged (the `moved`/
  `title_changed` flags from versioning are shown on the source for that reason).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DocumentVersion, NodeVersion
from app.generation.validate import _canon
from app.versioning.diff import numbers_in, word_diff

STATUS_ORDER = ["current", "review", "stale"]          # generation roll-up: worst wins
SEVERITY_ORDER = ["none", "low", "medium", "high"]


def classify_test_case(
    quote: str,
    expected_result: str,
    cited: list[dict],
) -> dict:
    """Pure function. `cited`: one entry per cited source:
        {"status": "unchanged"|"changed"|"removed", "number": "3.2",
         "current_text": str | None, "numbers_removed": [...], "numbers_added": [...],
         "changes": [{"op", "old", "new"}, ...]}   (word-diff spans of the section edit)
    """
    removed = [c for c in cited if c["status"] == "removed"]
    changed = [c for c in cited if c["status"] == "changed"]
    if removed:
        return {
            "status": "stale",
            "severity": "high",
            "quote_still_present": None,
            "reasons": [f"cited section {c['number']} no longer exists in the latest version" for c in removed],
        }
    if not changed:
        return {"status": "current", "severity": "none", "quote_still_present": True, "reasons": []}

    current = " ".join(_canon(c["current_text"] or "") for c in cited)
    quote_present = _canon(quote) in current
    removed_numbers = {n for c in changed for n in c["numbers_removed"]}
    depends_on = set(numbers_in(quote)) | set(numbers_in(expected_result))
    hit = sorted(depends_on & removed_numbers)
    where = ", ".join(c["number"] or "?" for c in changed)

    if not quote_present:
        reasons = [f"the quoted requirement no longer appears in section {where}"]
        if hit:
            reasons.append("value(s) this test depends on changed: " + "; ".join(_replacements(hit, changed)))
            severity = "high"
        else:
            severity = "medium"
        return {"status": "stale", "severity": severity, "quote_still_present": False, "reasons": reasons}

    reasons = [f"section {where} changed, but the quoted requirement is still present"]
    severity = "low"
    if hit:
        # quote intact, yet a number used in the expected result was changed elsewhere
        reasons.append(f"value(s) used in the expected result changed elsewhere in the section: {', '.join(hit)}")
        severity = "medium"
    return {"status": "review", "severity": severity, "quote_still_present": True, "reasons": reasons}


def _replacements(numbers: list[str], changed: list[dict]) -> list[str]:
    """Pair each changed value with what replaced it in the SAME edit: '2 -> 1.5', not every
    number the section gained (v2's 4.2 also gained E6, which has nothing to do with E3)."""
    out = []
    for n in numbers:
        new = None
        for c in changed:
            for ch in c.get("changes", []):
                if n in numbers_in(ch["old"]):
                    new = [x for x in numbers_in(ch["new"]) if x not in numbers_in(ch["old"])] or ["(removed)"]
                    break
            if new:
                break
        out.append(f"{n} -> {', '.join(new or ['?'])}")
    return out


def _latest_version(session: Session, document_id: int) -> DocumentVersion:
    return session.scalars(
        select(DocumentVersion).where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc()).limit(1)
    ).one()


def assess_generation(session: Session, doc: dict) -> dict:
    latest = _latest_version(session, doc["document_id"])
    logical_ids = [s["logical_node_id"] for s in doc["sources"]]
    rows = session.scalars(select(NodeVersion).where(
        NodeVersion.version_id == latest.id, NodeVersion.logical_node_id.in_(logical_ids)))
    current_by_logical = {r.logical_node_id: r for r in rows}
    pinned_by_id = {r.id: r for r in session.scalars(
        select(NodeVersion).where(NodeVersion.id.in_([s["node_id"] for s in doc["sources"]])))}

    sources_out = []
    cited_info: dict[int, dict] = {}            # by pinned node_id
    for s in doc["sources"]:
        cur: Optional[NodeVersion] = current_by_logical.get(s["logical_node_id"])
        entry = {
            "node_id": s["node_id"],
            "logical_node_id": s["logical_node_id"],
            "number": s["number"],
            "title": s["title"],
            "pinned_version": s["version"],
            "pinned_hash": s["content_hash"],
            "latest_version": latest.version_number,
            "latest_node_id": cur.id if cur else None,
            "latest_hash": cur.content_hash if cur else None,
            "latest_number": cur.number if cur else None,
            "latest_title": cur.title if cur else None,
            "diff": None,
        }
        if cur is None:
            entry["status"] = "removed"
        elif cur.content_hash == s["content_hash"]:
            entry["status"] = "unchanged"
        else:
            entry["status"] = "changed"
            old_body = pinned_by_id[s["node_id"]].body if s["node_id"] in pinned_by_id else ""
            d = word_diff(old_body, cur.body, max_changes=8)
            entry["diff"] = {k: d[k] for k in ("similarity", "numbers_removed", "numbers_added", "changes", "truncated")}
        sources_out.append(entry)
        cited_info[s["node_id"]] = {
            "status": entry["status"],
            "number": s["number"],
            "current_text": f"{cur.title} {cur.body}" if cur else None,
            "numbers_removed": entry["diff"]["numbers_removed"] if entry["diff"] else [],
            "numbers_added": entry["diff"]["numbers_added"] if entry["diff"] else [],
            "changes": entry["diff"]["changes"] if entry["diff"] else [],
        }

    cases_out = []
    for tc in doc.get("test_cases", []):
        cited = [cited_info[n] for n in tc["source_node_ids"] if n in cited_info]
        verdict = classify_test_case(tc["requirement_quote"], tc["expected_result"], cited)
        cases_out.append({"id": tc["id"], "title": tc["title"], **verdict})

    if cases_out:
        status = max((c["status"] for c in cases_out), key=STATUS_ORDER.index)
        severity = max((c["severity"] for c in cases_out), key=SEVERITY_ORDER.index)
    else:  # failed generation: judge by sources only
        changed = any(s["status"] != "unchanged" for s in sources_out)
        status, severity = ("review", "low") if changed else ("current", "none")

    counts = {k: sum(1 for c in cases_out if c["status"] == k) for k in STATUS_ORDER}
    return {
        "status": status,
        "severity": severity,
        "pinned_version": doc["pinned_version"],
        "checked_against_version": latest.version_number,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "summary": _summary(status, counts, doc["pinned_version"], latest.version_number),
        "test_case_counts": counts,
        "sources": sources_out,
        "test_cases": cases_out,
    }


def _summary(status: str, counts: dict, pinned: int, latest: int) -> str:
    if pinned == latest and status == "current":
        return f"generated from the latest version (v{latest})"
    if status == "current":
        return f"generated from v{pinned}; nothing it cites changed up to v{latest}"
    parts = [f"{counts[k]} {k}" for k in ("stale", "review", "current") if counts.get(k)]
    return f"generated from v{pinned}, checked against v{latest}: " + (", ".join(parts) or status)
