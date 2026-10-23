"""Ingest a PDF as a new version of a document.

    parse -> refuse if content was lost/duplicated -> match against latest version
          -> INSERT new DocumentVersion + NodeVersions (old rows are never modified)

Policies:
* Same file bytes as the LATEST version -> no new version (idempotent; returns the existing
  one). Same bytes as an OLDER version -> a new version is created: the manual was reverted,
  and that is a real event in the document's history.
* Coverage failure (unassigned or duplicated lines) -> IngestionError, nothing written.
  Structural irregularities (missing parent, out of order, duplicate titles) are expected in
  this document; they are stored as warnings and node flags, not treated as errors.
* The whole ingestion is one transaction: either every row is written or none is.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.db.models import Document, DocumentVersion, LogicalNode, NodeVersion
from app.parsing.models import ParseResult
from app.parsing.parser import parse_pdf
from app.versioning.matcher import MatchResult, NodeRef, match_versions


class IngestionError(Exception):
    pass


@dataclass
class IngestOutcome:
    version: DocumentVersion
    created: bool          # False when the file was identical to the latest version
    summary: dict


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def latest_version(session: Session, document_id: int) -> Optional[DocumentVersion]:
    return session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_number.desc())
        .limit(1)
    ).first()


def _refs_from_parse(result: ParseResult) -> list[NodeRef]:
    return [NodeRef(n.key, n.number, n.title, n.parent_key, n.content_hash, n.body) for n in result.nodes.values()]


def _refs_from_db(nodes: list[NodeVersion]) -> list[NodeRef]:
    by_id = {n.id: n for n in nodes}
    return [
        NodeRef(n.key, n.number, n.title, by_id[n.parent_id].key if n.parent_id else None, n.content_hash, n.body)
        for n in nodes
    ]


def _summary(match: Optional[MatchResult], prev: Optional[DocumentVersion], old_by_key: dict) -> dict:
    if match is None:
        return {"previous_version": None}
    return {
        "previous_version": prev.version_number if prev else None,
        "counts": {
            "unchanged": len(match.by_status("unchanged")),
            "modified": len(match.by_status("modified")),
            "added": len(match.by_status("added")),
            "removed": len(match.removed),
        },
        "modified": match.by_status("modified"),
        "added": match.by_status("added"),
        "removed": [
            {"key": k, "title": old_by_key[k].title, "logical_node_id": old_by_key[k].logical_node_id}
            for k in match.removed
        ],
        "moved_or_renamed": [
            k for k, m in match.matches.items() if m.old_key and (m.moved or m.title_changed)
        ],
        "matcher_notes": match.notes,
    }


def ingest_pdf(
    session: Session,
    pdf_path: str,
    document_name: str = config.DEFAULT_DOCUMENT_NAME,
    force_ocr: bool = False,
    source_name: Optional[str] = None,
) -> IngestOutcome:
    """source_name: original filename when pdf_path is a temporary upload."""
    sha = file_sha256(pdf_path)
    source_name = source_name or os.path.basename(pdf_path)

    doc = session.scalars(select(Document).where(Document.name == document_name)).first()
    if doc is None:
        doc = Document(name=document_name)
        session.add(doc)
        session.flush()

    prev = latest_version(session, doc.id)
    if prev is not None and prev.file_sha256 == sha:
        return IngestOutcome(prev, created=False, summary=prev.diff_summary)

    result = parse_pdf(pdf_path, force_ocr=force_ocr)
    if result.unassigned_lines or result.duplicated_line_ids:
        session.rollback()
        raise IngestionError(
            f"parser coverage check failed for {source_name}: "
            f"{len(result.unassigned_lines)} unassigned and {len(result.duplicated_line_ids)} duplicated lines; "
            "refusing to store an incomplete tree. Details: " + "; ".join(result.warnings[:5])
        )

    number = (session.scalar(select(func.max(DocumentVersion.version_number))
                             .where(DocumentVersion.document_id == doc.id)) or 0) + 1
    version = DocumentVersion(
        document_id=doc.id,
        version_number=number,
        source_filename=source_name,
        file_sha256=sha,
        page_count=result.page_count,
        parse_warnings=result.warnings,
    )
    session.add(version)
    session.flush()

    old_nodes = list(prev.nodes) if prev else []
    old_by_key = {n.key: n for n in old_nodes}
    match = match_versions(_refs_from_db(old_nodes), _refs_from_parse(result)) if prev else None

    rows: dict[str, NodeVersion] = {}
    for position, node in enumerate(result.nodes.values()):
        m = match.matches[node.key] if match else None
        predecessor = old_by_key.get(m.old_key) if (m and m.old_key) else None
        if predecessor is not None:
            logical_id = predecessor.logical_node_id
        else:
            logical = LogicalNode(document_id=doc.id, first_version_id=version.id)
            session.add(logical)
            session.flush()
            logical_id = logical.id

        row = NodeVersion(
            logical_node_id=logical_id,
            version_id=version.id,
            previous_id=predecessor.id if predecessor else None,
            key=node.key,
            number=node.number,
            title=node.title,
            level=node.level,
            position=position,
            page=node.page,
            body=node.body,
            blocks=[asdict(b) for b in node.blocks],
            content_hash=node.content_hash,
            flags=node.flags,
            change_status=m.status if m else "added",
            title_changed=m.title_changed if m else False,
            moved=m.moved if m else False,
            match_method=m.method if m else None,
            match_score=m.score if m else None,
        )
        session.add(row)
        rows[node.key] = row
    session.flush()

    for node in result.nodes.values():               # parents need ids, so second pass
        if node.parent_key:
            rows[node.key].parent_id = rows[node.parent_key].id

    version.diff_summary = _summary(match, prev, old_by_key)
    session.commit()
    return IngestOutcome(version, created=True, summary=version.diff_summary)
