"""FastAPI application.

Run:  uvicorn app.main:app --reload      then open http://127.0.0.1:8000/docs
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import documents, nodes, selections
from app.db.base import init_db
from app.services.ingestion import IngestionError
from app.services.queries import BadRequest, NotFound
from app.services.selections import Conflict


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="CT-200 QA Traceability API",
    version="0.3.0",
    description="Versioned section tree of the CT-200 manual, version-pinned selections, "
                "and (next stages) LLM-generated test cases with staleness detection.",
    lifespan=lifespan,
)

app.include_router(documents.router)
app.include_router(nodes.router)
app.include_router(selections.router)


# Service-layer errors -> HTTP. Services never import FastAPI, so they stay reusable.
@app.exception_handler(NotFound)
async def _not_found(_r: Request, e: NotFound):
    return JSONResponse(status_code=404, content={"detail": str(e)})


@app.exception_handler(BadRequest)
async def _bad_request(_r: Request, e: BadRequest):
    return JSONResponse(status_code=422, content={"detail": str(e)})


@app.exception_handler(Conflict)
async def _conflict(_r: Request, e: Conflict):
    return JSONResponse(status_code=409, content={"detail": str(e)})


@app.exception_handler(IngestionError)
async def _ingestion(_r: Request, e: IngestionError):
    return JSONResponse(status_code=422, content={"detail": str(e)})


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}
