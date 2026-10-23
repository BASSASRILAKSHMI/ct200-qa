import os
import shutil
import tempfile
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy.orm import Session

from app import config
from app.api.converters import version_out
from app.api.deps import get_session
from app.api.schemas import IngestOut, VersionOut
from app.services import queries
from app.services.ingestion import ingest_pdf

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post(
    "/ingest",
    response_model=IngestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a PDF as the next version of the manual",
    description=(
        "Parses the PDF and stores it as a new version. Earlier versions are never modified. "
        "Returns 200 with created=false if the file is identical to the latest version, "
        "and 422 if the parser could not account for every line of the PDF."
    ),
)
def ingest(
    response: Response,
    file: UploadFile = File(...),
    document: Optional[str] = Form(None),
    session: Session = Depends(get_session),
) -> IngestOut:
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="upload must be a .pdf file")
    fd, tmp = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(file.file, out)
        result = ingest_pdf(
            session, tmp, document_name=document or config.DEFAULT_DOCUMENT_NAME, source_name=file.filename
        )
    finally:
        os.remove(tmp)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return IngestOut(created=result.created, version=version_out(result.version))


@router.get("/versions", response_model=List[VersionOut], summary="All ingested versions, oldest first")
def list_versions(document: Optional[str] = None, session: Session = Depends(get_session)) -> List[VersionOut]:
    doc = queries.get_document(session, document)
    return [version_out(v) for v in doc.versions]


@router.get("/versions/{version}", response_model=VersionOut, summary="One version, including its diff summary")
def get_version(version: int, document: Optional[str] = None, session: Session = Depends(get_session)) -> VersionOut:
    doc = queries.get_document(session, document)
    return version_out(queries.get_version(session, doc, version))
