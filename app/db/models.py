"""Relational model for documents, versions and the section tree.

Two kinds of node identity, on purpose:

    LogicalNode   "section 4.2 Error Codes" as a concept, stable across versions.
    NodeVersion   that section's exact text in ONE version. Never updated after insert.

    Document 1--* DocumentVersion 1--* NodeVersion *--1 LogicalNode
                                        |  parent_id -> NodeVersion (same version)
                                        |  previous_id -> NodeVersion (same logical node, earlier version)

Re-ingesting v2 only INSERTS rows (a new DocumentVersion and its NodeVersions), so every
v1 NodeVersion keeps its text forever. Selections and generated test cases (later stages)
point at NodeVersion ids, which is what makes them version-pinned.

Annotations use Optional[...] (not "X | None") so the models work on Python 3.10.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document", order_by="DocumentVersion.version_number"
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    source_filename: Mapped[str] = mapped_column(String(500))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    page_count: Mapped[int] = mapped_column(Integer)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    parse_warnings: Mapped[list] = mapped_column(JSON, default=list)
    # counts and removed logical ids vs. the previous version, filled at ingestion
    diff_summary: Mapped[dict] = mapped_column(JSON, default=dict)

    document: Mapped[Document] = relationship(back_populates="versions")
    nodes: Mapped[list[NodeVersion]] = relationship(
        back_populates="version", order_by="NodeVersion.position"
    )


class LogicalNode(Base):
    __tablename__ = "logical_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    first_version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    versions: Mapped[list[NodeVersion]] = relationship(
        back_populates="logical", order_by="NodeVersion.version_id"
    )


class NodeVersion(Base):
    __tablename__ = "node_versions"
    __table_args__ = (
        UniqueConstraint("version_id", "logical_node_id"),
        UniqueConstraint("version_id", "key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    logical_node_id: Mapped[int] = mapped_column(ForeignKey("logical_nodes.id"), index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), index=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("node_versions.id"), nullable=True)
    previous_id: Mapped[Optional[int]] = mapped_column(ForeignKey("node_versions.id"), nullable=True)

    # structure, straight from the parser
    key: Mapped[str] = mapped_column(String(100))          # parser key, e.g. "4.2", "doc"
    number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    title: Mapped[str] = mapped_column(String(500))
    level: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)         # document order within the version
    page: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text, default="")
    blocks: Mapped[list] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    flags: Mapped[list] = mapped_column(JSON, default=list)

    # how this row relates to the previous version (filled by the matcher)
    change_status: Mapped[str] = mapped_column(String(20))  # added | unchanged | modified
    title_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    moved: Mapped[bool] = mapped_column(Boolean, default=False)  # number or parent changed
    match_method: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    version: Mapped[DocumentVersion] = relationship(back_populates="nodes")
    logical: Mapped[LogicalNode] = relationship(back_populates="versions")
    parent: Mapped[Optional[NodeVersion]] = relationship(
        remote_side="NodeVersion.id", foreign_keys=[parent_id]
    )
    previous: Mapped[Optional[NodeVersion]] = relationship(
        remote_side="NodeVersion.id", foreign_keys=[previous_id]
    )
