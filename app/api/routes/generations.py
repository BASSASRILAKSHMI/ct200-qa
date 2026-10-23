from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_llm, get_session, get_store
from app.api.schemas import GenerationOut
from app.generation.service import generate_for_selection
from app.llm.client import LLMClient
from app.services.queries import BadRequest, NotFound, get_node
from app.services.selections import get_selection
from app.staleness.assess import assess_generation
from app.storage.generations import GenerationStore

router = APIRouter(tags=["generation"])

STATUS_FILTER_HELP = "only return generations whose staleness status is this: current | review | stale"


def generation_out(
    doc: dict,
    session: Optional[Session] = None,
    include_raw: bool = False,
    reused=None,
    node_logical_id: Optional[int] = None,
) -> GenerationOut:
    attempts = [dict(a) for a in doc.get("attempts", [])]
    if not include_raw:
        for a in attempts:
            a["raw"] = None
    staleness = assess_generation(session, doc) if session is not None else None
    verdicts = {c["id"]: c for c in (staleness or {}).get("test_cases", [])}
    cases = [
        {**tc, "stale_status": verdicts.get(tc["id"], {}).get("status"),
         "stale_severity": verdicts.get(tc["id"], {}).get("severity")}
        for tc in doc.get("test_cases", [])
    ]
    node_cases = None
    if node_logical_id is not None:
        ids = {s["node_id"] for s in doc["sources"] if s["logical_node_id"] == node_logical_id}
        node_cases = [tc["id"] for tc in doc.get("test_cases", []) if ids & set(tc["source_node_ids"])]
    return GenerationOut(**{
        **doc, "id": doc["_id"], "attempts": attempts, "reused": reused, "test_cases": cases,
        "staleness": staleness, "node_test_case_ids": node_cases,
    })


def _filter(outs: List[GenerationOut], status_filter: Optional[str]) -> List[GenerationOut]:
    if status_filter is None:
        return outs
    if status_filter not in ("current", "review", "stale"):
        raise BadRequest("status must be one of: current, review, stale")
    return [o for o in outs if o.staleness and o.staleness.status == status_filter]


@router.post(
    "/selections/{selection_id}/generate",
    response_model=GenerationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Generate 3-5 QA test cases for a selection",
    description=(
        "201: new generation succeeded. 200: an identical earlier request already succeeded, its "
        "stored result is returned (reused=true) and the LLM is not called; pass force=true to "
        "generate again (the old result is kept and referenced by `supersedes`). 502: the LLM "
        "never produced a valid reply; the failed generation, with every attempt, is still "
        "stored and returned so it can be inspected. The response includes staleness against "
        "the latest version (relevant when the selection is pinned to an older version)."
    ),
)
def generate(
    selection_id: int,
    response: Response,
    force: bool = Query(False, description="ignore an identical earlier result and call the LLM again"),
    include_raw: bool = Query(False, description="include raw LLM replies in attempts"),
    session: Session = Depends(get_session),
    store: GenerationStore = Depends(get_store),
    llm: LLMClient = Depends(get_llm),
) -> GenerationOut:
    doc, reused = generate_for_selection(session, store, llm, selection_id, force=force)
    if reused:
        response.status_code = status.HTTP_200_OK
    elif doc["status"] == "failed":
        response.status_code = status.HTTP_502_BAD_GATEWAY
    return generation_out(doc, session, include_raw, reused)


@router.get(
    "/generations/{generation_id}",
    response_model=GenerationOut,
    summary="One stored generation, with its staleness against the latest version",
)
def get_generation(
    generation_id: str,
    include_raw: bool = Query(False),
    session: Session = Depends(get_session),
    store: GenerationStore = Depends(get_store),
) -> GenerationOut:
    doc = store.get(generation_id)
    if doc is None:
        raise NotFound(f"generation {generation_id} not found")
    return generation_out(doc, session, include_raw)


@router.get(
    "/selections/{selection_id}/generations",
    response_model=List[GenerationOut],
    summary="All generations for a selection, newest first, each with staleness",
)
def list_for_selection(
    selection_id: int,
    status_filter: Optional[str] = Query(None, alias="status", description=STATUS_FILTER_HELP),
    session: Session = Depends(get_session),
    store: GenerationStore = Depends(get_store),
) -> List[GenerationOut]:
    get_selection(session, selection_id)        # 404 if the selection does not exist
    outs = [generation_out(d, session) for d in store.find(selection_id=selection_id)]
    return _filter(outs, status_filter)


@router.get(
    "/nodes/{node_id}/generations",
    response_model=List[GenerationOut],
    summary="Generations whose test cases came from this section, in ANY version, with staleness",
    description=(
        "Matches on the section's logical id, so asking with the v2 node of 3.2 also returns test "
        "cases generated from v1's 3.2, which is exactly where stale ones are found. Set "
        "exact_version=true to match only this node version. node_test_case_ids lists which test "
        "cases in each generation cite this section."
    ),
)
def list_for_node(
    node_id: int,
    exact_version: bool = Query(False, description="only generations made from this exact node version"),
    status_filter: Optional[str] = Query(None, alias="status", description=STATUS_FILTER_HELP),
    session: Session = Depends(get_session),
    store: GenerationStore = Depends(get_store),
) -> List[GenerationOut]:
    node = get_node(session, node_id)           # 404 if unknown
    docs = (store.find(node_id=node.id) if exact_version
            else store.find(logical_node_id=node.logical_node_id))
    outs = [generation_out(d, session, node_logical_id=node.logical_node_id) for d in docs]
    return _filter(outs, status_filter)
