"""Request/response models. Optional/List typing keeps them valid on Python 3.10."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class VersionOut(BaseModel):
    version: int
    source_filename: str
    file_sha256: str
    page_count: int
    node_count: int
    ingested_at: datetime
    diff_summary: Dict[str, Any]
    parse_warnings: List[str]


class IngestOut(BaseModel):
    created: bool = Field(description="False if the file was identical to the latest version")
    version: VersionOut


class NodeSummary(BaseModel):
    id: int = Field(description="node id = this section in this version (immutable)")
    logical_node_id: int = Field(description="same value for this section in every version")
    version: int
    number: Optional[str]
    title: str
    level: int
    page: int
    content_hash: str
    change_status: str = Field(description="vs. previous version: added | unchanged | modified")
    flags: List[str]


class NodeDetail(NodeSummary):
    body: str
    blocks: List[Dict[str, Any]]
    title_changed: bool
    moved: bool
    match_method: Optional[str]
    parent: Optional[NodeSummary]
    path: List[NodeSummary] = Field(description="ancestors, root first")
    children: List[NodeSummary]


class TreeNode(BaseModel):
    id: int
    number: Optional[str]
    title: str
    level: int
    change_status: str
    flags: List[str]
    children: List["TreeNode"] = []


class SearchHit(BaseModel):
    node: NodeSummary
    snippet: str


class SearchOut(BaseModel):
    version: int
    query: Optional[str]
    count: int
    hits: List[SearchHit]


class HistoryEntry(BaseModel):
    version: int
    node_id: int
    number: Optional[str]
    title: str
    content_hash: str
    change_status: str
    match_method: Optional[str]


class ChangeReport(BaseModel):
    node_id: int
    logical_node_id: int
    version: int
    latest_version: int
    status_vs_latest: str = Field(description="is_latest | unchanged | changed | removed")
    changed: bool
    latest_node_id: Optional[int]
    diff_to_latest: Optional[Dict[str, Any]]
    history: List[HistoryEntry]


class SelectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    node_ids: List[int] = Field(min_length=1, description="node ids (version-specific), all from one version")
    description: Optional[str] = None
    include_descendants: bool = Field(
        False, description="also pin every section under the chosen ones (chapter headings have no text)"
    )


class SelectionItemOut(BaseModel):
    node_id: int
    logical_node_id: int
    number: Optional[str]
    title: str
    content_hash: str
    explicit: bool = Field(description="False if added by include_descendants")
    status_in_latest: str = Field(description="is_latest | unchanged | changed | removed")


class SelectionOut(BaseModel):
    id: int
    name: str
    description: Optional[str]
    pinned_version: int
    latest_version: int
    fingerprint: str
    created_at: datetime
    items: List[SelectionItemOut]
    text: Optional[str] = Field(None, description="exact pinned text (include_text=true)")
