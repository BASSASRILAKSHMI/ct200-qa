"""Create and read version-pinned selections.

Rules (each one is a decision, not an accident):
* Items are NodeVersion ids -> pinned to exact text; re-ingestion can never change them.
* All items must belong to the SAME document version. Mixing v1 and v2 rows would make a
  set of test cases describe a manual that never existed. A mixed request is rejected (422).
* include_descendants=True expands a chosen heading to its whole subtree at creation time,
  because chapter headings ("4. Alarms and Safety Behavior") have no body text of their own.
  The expansion is stored, so the selection does not silently grow later.
* Names are unique per document; reusing one is a 409 (the caller decides: rename or reuse).
* Order is document order, duplicates are collapsed, unknown ids are a 404 listing them all.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import NodeVersion, Selection, SelectionItem
from app.services.queries import BadRequest, Conflict, NotFound, descendants_of, latest_version


def fingerprint(nodes: list[NodeVersion]) -> str:
    parts = sorted(f"{n.id}:{n.content_hash}" for n in nodes)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def create_selection(
    session: Session,
    name: str,
    node_ids: list[int],
    description: Optional[str] = None,
    include_descendants: bool = False,
) -> Selection:
    name = name.strip()
    if not name:
        raise BadRequest("name must not be empty")
    if not node_ids:
        raise BadRequest("node_ids must contain at least one id")

    unique_ids = list(dict.fromkeys(node_ids))
    found = {n.id: n for n in session.scalars(select(NodeVersion).where(NodeVersion.id.in_(unique_ids)))}
    missing = [i for i in unique_ids if i not in found]
    if missing:
        raise NotFound(f"unknown node ids: {missing}")

    versions = {n.version_id for n in found.values()}
    if len(versions) > 1:
        detail = sorted({(n.version.version_number, n.id) for n in found.values()})
        raise BadRequest(
            "all nodes in a selection must come from the same document version; got "
            + ", ".join(f"node {i} (v{v})" for v, i in detail)
        )

    chosen: dict[int, tuple[NodeVersion, bool]] = {i: (found[i], True) for i in unique_ids}
    if include_descendants:
        for n in list(found.values()):
            for d in descendants_of(session, n):
                chosen.setdefault(d.id, (d, False))

    ordered = sorted(chosen.values(), key=lambda pair: pair[0].position)
    nodes = [n for n, _ in ordered]
    first = nodes[0]
    sel = Selection(
        name=name,
        description=description,
        document_id=first.version.document_id,
        version_id=first.version_id,
        fingerprint=fingerprint(nodes),
        items=[SelectionItem(node_version_id=n.id, position=i, explicit=explicit)
               for i, (n, explicit) in enumerate(ordered)],
    )
    session.add(sel)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise Conflict(f"a selection named {name!r} already exists for this document")
    return sel


def get_selection(session: Session, selection_id: int) -> Selection:
    sel = session.get(Selection, selection_id)
    if sel is None:
        raise NotFound(f"selection {selection_id} not found")
    return sel


def list_selections(session: Session) -> list[Selection]:
    return list(session.scalars(select(Selection).order_by(Selection.id)))


def selection_text(sel: Selection) -> str:
    """The exact text the selection was created against, in document order. This is what
    the LLM stage will send, and it never changes after creation."""
    parts = []
    for item in sel.items:
        n = item.node
        heading = f"{n.number} {n.title}".strip() if n.number else n.title
        parts.append(f"## [node {n.id}] {heading}\n{n.body}".rstrip())
    return "\n\n".join(parts)


def latest_status(session: Session, sel: Selection) -> dict:
    """Light preview of drift: is each pinned node still the same in the latest version?
    (Full staleness of generated test cases is a later stage.)"""
    latest = latest_version(session, sel.document_id)
    out = {"latest_version": latest.version_number, "pinned_version": sel.version.version_number, "items": {}}
    if latest.id == sel.version_id:
        for item in sel.items:
            out["items"][item.node_version_id] = "is_latest"
        return out
    rows = session.scalars(select(NodeVersion).where(
        NodeVersion.version_id == latest.id,
        NodeVersion.logical_node_id.in_([i.node.logical_node_id for i in sel.items]),
    ))
    by_logical = {r.logical_node_id: r for r in rows}
    for item in sel.items:
        cur = by_logical.get(item.node.logical_node_id)
        if cur is None:
            out["items"][item.node_version_id] = "removed"
        elif cur.content_hash == item.node.content_hash and cur.title == item.node.title:
            out["items"][item.node_version_id] = "unchanged"
        else:
            out["items"][item.node_version_id] = "changed"
    return out
