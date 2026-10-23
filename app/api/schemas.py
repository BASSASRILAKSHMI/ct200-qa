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


# ---- generation ---------------------------------------------------------------------------

class GeneratedTestCase(BaseModel):
    id: str
    stale_status: Optional[str] = Field(None, description="current | review | stale (vs. latest version)")
    stale_severity: Optional[str] = Field(None, description="none | low | medium | high")
    title: str
    type: str
    priority: str
    source_node_ids: List[int]
    requirement_quote: str
    preconditions: List[str]
    steps: List[str]
    expected_result: str
    unverified_values: List[str] = Field(
        default_factory=list, description="values in expected_result that the cited sections do not state")


class GenerationSource(BaseModel):
    node_id: int
    logical_node_id: int
    version: int
    number: Optional[str]
    title: str
    content_hash: str


class GenerationAttempt(BaseModel):
    attempt: int
    accepted: bool
    finish_reason: Optional[str]
    latency_ms: int
    repaired: Optional[str]
    errors: List[str]
    raw: Optional[str] = Field(None, description="raw LLM reply (include_raw=true)")


class TestCaseStaleness(BaseModel):
    id: str
    title: str
    status: str = Field(description="current | review | stale")
    severity: str = Field(description="none | low | medium | high")
    quote_still_present: Optional[bool]
    reasons: List[str]


class SourceStaleness(BaseModel):
    node_id: int
    logical_node_id: int
    number: Optional[str]
    title: str
    status: str = Field(description="unchanged | changed | removed")
    pinned_version: int
    pinned_hash: str
    latest_version: int
    latest_node_id: Optional[int]
    latest_hash: Optional[str]
    latest_number: Optional[str]
    latest_title: Optional[str]
    diff: Optional[Dict[str, Any]]


class Staleness(BaseModel):
    status: str = Field(description="worst test-case status: current | review | stale")
    severity: str = Field(description="worst severity: none | low | medium | high")
    pinned_version: int
    checked_against_version: int
    checked_at: str
    summary: str
    test_case_counts: Dict[str, int]
    sources: List[SourceStaleness]
    test_cases: List[TestCaseStaleness]


class GenerationOut(BaseModel):
    id: str
    status: str = Field(description="succeeded | failed")
    reused: Optional[bool] = Field(None, description="POST only: true if an identical earlier result was returned")
    selection_id: int
    selection_name: str
    pinned_version: int
    prompt_version: str
    provider: str
    model: str
    forced: bool
    supersedes: Optional[str]
    created_at: str
    failure_reason: Optional[str]
    failure_detail: Optional[str]
    warnings: List[str]
    sources: List[GenerationSource]
    test_cases: List[GeneratedTestCase]
    attempts: List[GenerationAttempt]
    staleness: Optional[Staleness] = Field(None, description="computed now, against the latest version")
    node_test_case_ids: Optional[List[str]] = Field(
        None, description="GET /nodes/{id}/generations only: test cases that cite this section")
