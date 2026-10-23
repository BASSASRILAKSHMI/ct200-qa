"""Read-side queries over documents, versions and nodes. No HTTP here, so the API and
later stages (staleness, retrieval) share one implementation."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import config
from app.db.models import Document, DocumentVersion, NodeVersion
from app.versioning.diff import word_diff


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


# ---- documents and versions ---------------------------------------------------------------

def get_document(session: Session, name: Optional[str] = None) -> Document:
    name = name or config.DEFAULT_DOCUMENT_NAME
    doc = session.scalars(select(Document).where(Document.name == name)).first()
    if doc is None:
        raise NotFound(f"document {name!r} has not been ingested yet")
    return doc


def get_version(session: Session, doc: Document, number: Optional[int] = None) -> DocumentVersion:
    q = select(DocumentVersion).where(DocumentVersion.document_id == doc.id)
    if number is None:
        q = q.order_by(DocumentVersion.version_number.desc()).limit(1)
    else:
        q = q.where(DocumentVersion.version_number == number)
    v = session.scalars(q).first()
    if v is None:
        raise NotFound(f"version {number} of {doc.name!r} not found" if number else f"{doc.name!r} has no versions")
    return v


def latest_version(session: Session, document_id: int) -> DocumentVersion:
    return session.scalars(
        select(DocumentVersion).where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc()).limit(1)
    ).one()


# ---- nodes --------------------------------------------------------------------------------

def get_node(session: Session, node_id: int) -> NodeVersion:
    node = session.get(NodeVersion, node_id)
    if node is None:
        raise NotFound(f"node {node_id} not found")
    return node


def root_of(session: Session, version: DocumentVersion) -> NodeVersion:
    return session.scalars(
        select(NodeVersion).where(NodeVersion.version_id == version.id, NodeVersion.parent_id.is_(None))
    ).one()


def children_of(session: Session, node: NodeVersion) -> list[NodeVersion]:
    return list(session.scalars(
        select(NodeVersion).where(NodeVersion.parent_id == node.id).order_by(NodeVersion.position)
    ))


def descendants_of(session: Session, node: NodeVersion) -> list[NodeVersion]:
    out: list[NodeVersion] = []
    stack = children_of(session, node)[::-1]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(children_of(session, n)[::-1])
    return out


def path_of(session: Session, node: NodeVersion) -> list[NodeVersion]:
    """Ancestors from the root down to (excluding) the node."""
    chain: list[NodeVersion] = []
    cur = node.parent
    while cur is not None:
        chain.append(cur)
        cur = cur.parent
    return chain[::-1]


def search(
    session: Session,
    version: DocumentVersion,
    q: Optional[str] = None,
    field: str = "all",
    flag: Optional[str] = None,
    limit: int = 50,
) -> list[NodeVersion]:
    if field not in ("all", "title", "body"):
        raise BadRequest("field must be one of: all, title, body")
    if not q and not flag:
        raise BadRequest("give a search term (q) and/or a flag filter")
    stmt = select(NodeVersion).where(NodeVersion.version_id == version.id)
    if q:
        like = f"%{q}%"     # SQLite LIKE is case-insensitive for ASCII
        cond = {
            "title": NodeVersion.title.ilike(like) | NodeVersion.number.ilike(like),
            "body": NodeVersion.body.ilike(like),
            "all": or_(NodeVersion.title.ilike(like), NodeVersion.number.ilike(like), NodeVersion.body.ilike(like)),
        }[field]
        stmt = stmt.where(cond)
    rows = list(session.scalars(stmt.order_by(NodeVersion.position)))
    if flag:  # flags are a JSON list; filter in Python (prefix match, e.g. "out_of_order")
        rows = [r for r in rows if any(f == flag or f.startswith(flag + ":") for f in r.flags)]
    return rows[:limit]


def snippet(text: str, q: Optional[str], width: int = 80) -> str:
    if not text:
        return ""
    if not q or q.lower() not in text.lower():
        return text[:width] + ("…" if len(text) > width else "")
    i = text.lower().index(q.lower())
    start, end = max(0, i - width // 2), min(len(text), i + len(q) + width // 2)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


# ---- change history -----------------------------------------------------------------------

def history(session: Session, logical_node_id: int) -> list[NodeVersion]:
    return list(session.scalars(
        select(NodeVersion).join(DocumentVersion)
        .where(NodeVersion.logical_node_id == logical_node_id)
        .order_by(DocumentVersion.version_number)
    ))


def change_report(session: Session, node: NodeVersion) -> dict:
    """Has this section changed since the given node version? Compared with the latest
    version of the document, plus the full per-version history of the logical section."""
    rows = history(session, node.logical_node_id)
    latest_doc_version = latest_version(session, node.version.document_id)
    in_latest = next((r for r in rows if r.version_id == latest_doc_version.id), None)

    if in_latest is None:
        status = "removed"            # section no longer exists in the latest version
    elif in_latest.id == node.id:
        status = "is_latest"
    elif in_latest.content_hash == node.content_hash and in_latest.title == node.title:
        status = "unchanged"
    else:
        status = "changed"

    report = {
        "node_id": node.id,
        "logical_node_id": node.logical_node_id,
        "version": node.version.version_number,
        "latest_version": latest_doc_version.version_number,
        "status_vs_latest": status,
        "changed": status in ("changed", "removed"),
        "latest_node_id": in_latest.id if in_latest else None,
        "diff_to_latest": None,
        "history": [
            {
                "version": r.version.version_number,
                "node_id": r.id,
                "number": r.number,
                "title": r.title,
                "content_hash": r.content_hash,
                "change_status": r.change_status,
                "match_method": r.match_method,
            }
            for r in rows
        ],
    }
    if status == "changed":
        d = word_diff(node.body, in_latest.body)
        d["title_old"], d["title_new"] = node.title, in_latest.title
        d["number_old"], d["number_new"] = node.number, in_latest.number
        report["diff_to_latest"] = d
    return report
