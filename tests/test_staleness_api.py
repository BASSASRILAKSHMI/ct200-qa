"""Retrieval API: staleness is visible where test cases are fetched (by selection, node, id)."""
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


def upload(c, path):
    with open(path, "rb") as f:
        r = c.post("/documents/ingest", files={"file": ("m.pdf", f, "application/pdf")})
    assert r.status_code == 201


def nid(c, number, version):
    hits = c.get("/search", params={"q": number, "field": "title", "version": version}).json()["hits"]
    return next(h["node"]["id"] for h in hits if h["node"]["number"] == number)


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    """v1 -> selection 3.2+4.2 -> generate -> v2. Returns client and ids."""
    engine = make_engine(f"sqlite:///{tmp_path_factory.mktemp('sapi') / 's.db'}")
    init_db(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    store = InMemoryGenerationStore()
    script: list = []

    def session_override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_llm] = lambda: ScriptedLLM(script)
    c = TestClient(app)

    upload(c, V1)
    a, b = nid(c, "3.2", 1), nid(c, "4.2", 1)
    sel = c.post("/selections", json={"name": "inflation+errors", "node_ids": [a, b]}).json()

    def case(title, ids, quote):
        return {"title": title, "type": "boundary", "priority": "high", "source_node_ids": ids,
                "requirement_quote": quote, "preconditions": [], "steps": ["start"], "expected_result": quote}
    script.append(json.dumps({"test_cases": [
        case("Inflation target 180 mmHg", [a], "initial target of 180 mmHg"),
        case("Increments of 40 mmHg", [a], "inflates in 40 mmHg increments"),
        case("E3 deflation within 2 seconds", [b], "Auto-deflates within 2 seconds, displays E3"),
    ]}))
    gen = c.post(f"/selections/{sel['id']}/generate").json()
    current_before = c.get(f"/generations/{gen['id']}").json()
    upload(c, V2)
    yield {"c": c, "sel": sel["id"], "gen": gen["id"], "before": current_before,
           "v1_32": a, "v2_32": nid(c, "3.2", 2), "v2_71": nid(c, "7.1", 2)}
    app.dependency_overrides.clear()
    engine.dispose()


def test_generation_was_current_when_made(flow):
    st = flow["before"]["staleness"]
    assert st["status"] == "current" and st["checked_against_version"] == 1
    assert {tc["stale_status"] for tc in flow["before"]["test_cases"]} == {"current"}


def test_get_generation_shows_staleness_after_v2(flow):
    g = flow["c"].get(f"/generations/{flow['gen']}").json()
    assert g["staleness"]["status"] == "stale" and g["staleness"]["severity"] == "high"
    assert g["staleness"]["pinned_version"] == 1 and g["staleness"]["checked_against_version"] == 2
    inline = {tc["id"]: (tc["stale_status"], tc["stale_severity"]) for tc in g["test_cases"]}
    assert inline == {"TC-1": ("review", "low"), "TC-2": ("stale", "high"), "TC-3": ("stale", "high")}
    src = {s["number"]: s for s in g["staleness"]["sources"]}
    assert src["4.2"]["diff"]["numbers_removed"] == ["2"] and src["4.2"]["latest_version"] == 2


def test_by_selection_with_status_filter(flow):
    c, sel = flow["c"], flow["sel"]
    assert [g["id"] for g in c.get(f"/selections/{sel}/generations", params={"status": "stale"}).json()] == [flow["gen"]]
    assert c.get(f"/selections/{sel}/generations", params={"status": "current"}).json() == []
    assert c.get(f"/selections/{sel}/generations", params={"status": "bogus"}).status_code == 422


def test_by_node_finds_v1_test_cases_from_the_v2_node(flow):
    c = flow["c"]
    hits = c.get(f"/nodes/{flow['v2_32']}/generations").json()
    assert [g["id"] for g in hits] == [flow["gen"]]
    assert hits[0]["node_test_case_ids"] == ["TC-1", "TC-2"]                 # cases citing 3.2
    assert c.get(f"/nodes/{flow['v2_32']}/generations", params={"exact_version": True}).json() == []
    exact = c.get(f"/nodes/{flow['v1_32']}/generations", params={"exact_version": True}).json()
    assert [g["id"] for g in exact] == [flow["gen"]]


def test_by_node_for_section_never_used_is_empty_and_unknown_node_is_404(flow):
    c = flow["c"]
    assert c.get(f"/nodes/{flow['v2_71']}/generations").json() == []
    assert c.get("/nodes/999999/generations").status_code == 404
