from typing import List

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.converters import selection_out
from app.api.deps import get_session
from app.api.schemas import SelectionCreate, SelectionOut
from app.services import selections as svc

router = APIRouter(prefix="/selections", tags=["selections"])


@router.post(
    "",
    response_model=SelectionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a named, version-pinned selection",
    description=(
        "node_ids are version-specific node ids and must all belong to one document version "
        "(422 otherwise). Unknown ids -> 404. A name already used -> 409."
    ),
)
def create(body: SelectionCreate, session: Session = Depends(get_session)) -> SelectionOut:
    sel = svc.create_selection(
        session, body.name, body.node_ids, body.description, body.include_descendants
    )
    return selection_out(session, sel, include_text=True)


@router.get("", response_model=List[SelectionOut], summary="All selections")
def list_all(session: Session = Depends(get_session)) -> List[SelectionOut]:
    return [selection_out(session, s) for s in svc.list_selections(session)]


@router.get("/{selection_id}", response_model=SelectionOut, summary="One selection; its pinned text never changes")
def get_one(
    selection_id: int,
    include_text: bool = Query(True, description="include the exact pinned text"),
    session: Session = Depends(get_session),
) -> SelectionOut:
    return selection_out(session, svc.get_selection(session, selection_id), include_text=include_text)
