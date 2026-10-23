"""ORM rows -> response models."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import DocumentVersion, NodeVersion, Selection
from app.services import queries
from app.services.selections import latest_status, selection_text

from .schemas import NodeDetail, NodeSummary, SelectionItemOut, SelectionOut, TreeNode, VersionOut


def utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on read; values are written in UTC, so label them as UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def version_out(v: DocumentVersion) -> VersionOut:
    return VersionOut(
        version=v.version_number,
        source_filename=v.source_filename,
        file_sha256=v.file_sha256,
        page_count=v.page_count,
        node_count=len(v.nodes),
        ingested_at=utc(v.ingested_at),
        diff_summary=v.diff_summary or {},
        parse_warnings=v.parse_warnings or [],
    )


def node_summary(n: NodeVersion) -> NodeSummary:
    return NodeSummary(
        id=n.id,
        logical_node_id=n.logical_node_id,
        version=n.version.version_number,
        number=n.number,
        title=n.title,
        level=n.level,
        page=n.page,
        content_hash=n.content_hash,
        change_status=n.change_status,
        flags=n.flags or [],
    )


def node_detail(session: Session, n: NodeVersion) -> NodeDetail:
    return NodeDetail(
        **node_summary(n).model_dump(),
        body=n.body,
        blocks=n.blocks or [],
        title_changed=n.title_changed,
        moved=n.moved,
        match_method=n.match_method,
        parent=node_summary(n.parent) if n.parent else None,
        path=[node_summary(p) for p in queries.path_of(session, n)],
        children=[node_summary(c) for c in queries.children_of(session, n)],
    )


def tree(session: Session, n: NodeVersion) -> TreeNode:
    return TreeNode(
        id=n.id, number=n.number, title=n.title, level=n.level,
        change_status=n.change_status, flags=n.flags or [],
        children=[tree(session, c) for c in queries.children_of(session, n)],
    )


def selection_out(session: Session, sel: Selection, include_text: bool = False) -> SelectionOut:
    status = latest_status(session, sel)
    return SelectionOut(
        id=sel.id,
        name=sel.name,
        description=sel.description,
        pinned_version=status["pinned_version"],
        latest_version=status["latest_version"],
        fingerprint=sel.fingerprint,
        created_at=utc(sel.created_at),
        items=[
            SelectionItemOut(
                node_id=i.node.id,
                logical_node_id=i.node.logical_node_id,
                number=i.node.number,
                title=i.node.title,
                content_hash=i.node.content_hash,
                explicit=i.explicit,
                status_in_latest=status["items"][i.node_version_id],
            )
            for i in sel.items
        ],
        text=selection_text(sel) if include_text else None,
    )
