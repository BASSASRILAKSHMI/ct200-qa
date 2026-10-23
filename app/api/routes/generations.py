from typing import List

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_llm, get_session, get_store
from app.api.schemas import GenerationOut
from app.generation.service import generate_for_selection
from app.llm.client import LLMClient
from app.services.queries import NotFound
from app.services.selections import get_selection
from app.storage.generations import GenerationStore

router = APIRouter(tags=["generation"])


def generation_out(doc: dict, include_raw: bool = False, reused=None) -> GenerationOut:
    attempts = [dict(a) for a in doc.get("attempts", [])]
    if not include_raw:
        for a in attempts:
            a["raw"] = None
    return GenerationOut(**{**doc, "id": doc["_id"], "attempts": attempts, "reused": reused})


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
        "stored and returned so it can be inspected."
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
    return generation_out(doc, include_raw, reused)


@router.get("/generations/{generation_id}", response_model=GenerationOut, summary="One stored generation")
def get_generation(
    generation_id: str,
    include_raw: bool = Query(False),
    store: GenerationStore = Depends(get_store),
) -> GenerationOut:
    doc = store.get(generation_id)
    if doc is None:
        raise NotFound(f"generation {generation_id} not found")
    return generation_out(doc, include_raw)


@router.get(
    "/selections/{selection_id}/generations",
    response_model=List[GenerationOut],
    summary="All generations for a selection, newest first",
)
def list_for_selection(
    selection_id: int,
    session: Session = Depends(get_session),
    store: GenerationStore = Depends(get_store),
) -> List[GenerationOut]:
    get_selection(session, selection_id)        # 404 if the selection does not exist
    return [generation_out(d) for d in store.find(selection_id=selection_id)]
