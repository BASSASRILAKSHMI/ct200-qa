from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.converters import node_detail, node_summary, tree
from app.api.deps import get_session
from app.api.schemas import ChangeReport, NodeDetail, NodeSummary, SearchHit, SearchOut, TreeNode
from app.services import queries

router = APIRouter(tags=["browse"])

VERSION_HELP = "version number (1, 2, ...); defaults to the latest version"


@router.get("/sections", response_model=List[NodeSummary], summary="Top-level sections of a version")
def top_level_sections(
    version: Optional[int] = Query(None, description=VERSION_HELP),
    document: Optional[str] = None,
    session: Session = Depends(get_session),
) -> List[NodeSummary]:
    doc = queries.get_document(session, document)
    v = queries.get_version(session, doc, version)
    root = queries.root_of(session, v)
    return [node_summary(c) for c in queries.children_of(session, root)]


@router.get("/tree", response_model=TreeNode, summary="Whole section tree of a version (headings only)")
def full_tree(
    version: Optional[int] = Query(None, description=VERSION_HELP),
    document: Optional[str] = None,
    session: Session = Depends(get_session),
) -> TreeNode:
    doc = queries.get_document(session, document)
    v = queries.get_version(session, doc, version)
    return tree(session, queries.root_of(session, v))


@router.get("/nodes/{node_id}", response_model=NodeDetail, summary="A node with full text, hash, parent and children")
def get_node(node_id: int, session: Session = Depends(get_session)) -> NodeDetail:
    return node_detail(session, queries.get_node(session, node_id))


@router.get(
    "/nodes/{node_id}/changes",
    response_model=ChangeReport,
    summary="Has this section changed since this version? History and diff to latest",
)
def node_changes(node_id: int, session: Session = Depends(get_session)) -> ChangeReport:
    return ChangeReport(**queries.change_report(session, queries.get_node(session, node_id)))


@router.get("/search", response_model=SearchOut, summary="Search headings and/or text, optionally filter by flag")
def search(
    q: Optional[str] = Query(None, description="case-insensitive text to find"),
    field: str = Query("all", description="all | title | body"),
    flag: Optional[str] = Query(None, description="e.g. duplicate_title, out_of_order, missing_parent"),
    version: Optional[int] = Query(None, description=VERSION_HELP),
    limit: int = Query(50, ge=1, le=200),
    document: Optional[str] = None,
    session: Session = Depends(get_session),
) -> SearchOut:
    doc = queries.get_document(session, document)
    v = queries.get_version(session, doc, version)
    rows = queries.search(session, v, q=q, field=field, flag=flag, limit=limit)
    hits = [SearchHit(node=node_summary(r), snippet=queries.snippet(r.body, q)) for r in rows]
    return SearchOut(version=v.version_number, query=q, count=len(hits), hits=hits)
