"""Generation over HTTP: status codes for new / reused / failed, and retrieval."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_llm, get_session, get_store
from app.db.base import init_db, make_engine
from app.llm.client import ScriptedLLM
from app.main import app
from app.storage.generations import InMemoryGenerationStore

from .conftest import V1, V2


class Harness:
    def __init__(self):
        self.script: list = []
        self.store = InMemoryGenerationStore()
        self.llm = None

    def make_llm(self):
        self.llm = ScriptedLLM(self.script, model="scripted-model")
        return self.llm


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    engine = make_engine(f"sqlite:///{tmp_path_factory.mktemp('genapi') / 'g.db'}")
    init_db(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    harness = Harness()

    def session_override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_store] = lambda: harness.store
    app.dependency_overrides[get_llm] = harness.make_llm
    harness.client = TestClient(app)
    for path in (V1, V2):
        with open(path, "rb") as f:
            assert harness.client.post("/documents/ingest", files={"file": ("m.pdf", f, "application/pdf")}).status_code == 201
    yield harness
    app.dependency_overrides.clear()
    engine.dispose()


def nid(h, number, version=1):
    hits = h.client.get("/search", params={"q": number, "field": "title", "version": version}).json()["hits"]
    return next(x["node"]["id"] for x in hits if x["node"]["number"] == number)


def reply(h):
    a, b = nid(h, "3.2"), nid(h, "4.2")

    def c(title, ids, quote):
        return {"title": title, "type": "boundary", "priority": "high", "source_node_ids": ids,
                "requirement_quote": quote, "preconditions": [], "steps": ["start a measurement"],
                "expected_result": "as stated in the manual"}
    return json.dumps({"test_cases": [
        c("Inflate to the 180 mmHg target", [a], "initial target of 180 mmHg"),
        c("Step up in 40 mmHg increments", [a], "inflates in 40 mmHg increments"),
        c("E3 auto-deflates within 2 seconds", [b], "Auto-deflates within 2 seconds"),
    ]})


@pytest.fixture()
def sel_id(h, request):
    r = h.client.post("/selections", json={"name": request.node.name, "node_ids": [nid(h, "3.2"), nid(h, "4.2")]})
    assert r.status_code == 201
    h.store = InMemoryGenerationStore()          # fresh store per test
    return r.json()["id"]


def test_new_then_duplicate_then_forced(h, sel_id):
    h.script = [reply(h)]
    first = h.client.post(f"/selections/{sel_id}/generate")
    assert first.status_code == 201 and first.json()["reused"] is False
    assert [t["id"] for t in first.json()["test_cases"]] == ["TC-1", "TC-2", "TC-3"]

    h.script = []                                                     # any LLM call would fail
    again = h.client.post(f"/selections/{sel_id}/generate")
    assert again.status_code == 200 and again.json()["reused"] is True
    assert again.json()["id"] == first.json()["id"] and h.llm.calls == []

    h.script = [reply(h)]
    forced = h.client.post(f"/selections/{sel_id}/generate", params={"force": True})
    assert forced.status_code == 201 and forced.json()["supersedes"] == first.json()["id"]

    listed = h.client.get(f"/selections/{sel_id}/generations").json()
    assert [g["id"] for g in listed] == [forced.json()["id"], first.json()["id"]]


def test_invalid_output_returns_502_but_the_failure_is_stored_and_inspectable(h, sel_id):
    h.script = ["not json", "still not json", "nope"]
    r = h.client.post(f"/selections/{sel_id}/generate")
    assert r.status_code == 502
    body = r.json()
    assert body["status"] == "failed" and body["failure_reason"] == "invalid_output"
    assert [a["raw"] for a in body["attempts"]] == [None, None, None]          # raw hidden by default
    stored = h.client.get(f"/generations/{body['id']}", params={"include_raw": True}).json()
    assert [a["raw"] for a in stored["attempts"]] == ["not json", "still not json", "nope"]


def test_retry_succeeds_and_attempt_history_is_visible(h, sel_id):
    h.script = ["```\nnot even json\n```", reply(h)]
    r = h.client.post(f"/selections/{sel_id}/generate").json()
    assert r["status"] == "succeeded"
    assert [a["accepted"] for a in r["attempts"]] == [False, True]
    assert "not valid JSON" in r["attempts"][0]["errors"][0]


def test_unknown_ids_are_404(h):
    assert h.client.post("/selections/987654/generate").status_code == 404
    assert h.client.get("/generations/doesnotexist").status_code == 404
    assert h.client.get("/selections/987654/generations").status_code == 404


def test_heading_only_selection_is_422_without_llm_call(h):
    s = h.client.post("/selections", json={"name": "just-a-heading", "node_ids": [nid(h, "4")]}).json()
    h.script = []
    assert h.client.post(f"/selections/{s['id']}/generate").status_code == 422


def test_timestamps_are_utc_labelled(h):
    v = h.client.get("/documents/versions").json()[0]
    assert v["ingested_at"].endswith("Z") or v["ingested_at"].endswith("+00:00")
