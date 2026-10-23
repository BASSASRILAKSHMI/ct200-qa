"""HTTP-level tests: v1 and v2 are uploaded through the API, then browsed and selected."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_session
from app.db.base import init_db, make_engine
from app.main import app

from .conftest import V1, V2


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    engine = make_engine(f"sqlite:///{tmp_path_factory.mktemp('api') / 'api.db'}")
    init_db(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_session] = override
    c = TestClient(app)          # no context manager: lifespan would touch the default DB
    for path in (V1, V2):
        with open(path, "rb") as f:
            r = c.post("/documents/ingest", files={"file": (path.split("/")[-1].split("\\")[-1], f, "application/pdf")})
        assert r.status_code == 201, r.text
    yield c
    app.dependency_overrides.clear()
    engine.dispose()


def node_id(client, number, version):
    hits = client.get("/search", params={"q": number, "field": "title", "version": version}).json()["hits"]
    exact = [h["node"]["id"] for h in hits if h["node"]["number"] == number]
    assert len(exact) == 1, hits
    return exact[0]


# ---- documents ----------------------------------------------------------------------------

def test_reuploading_identical_pdf_returns_200_not_a_new_version(client):
    with open(V2, "rb") as f:
        r = client.post("/documents/ingest", files={"file": ("ct200_manual_v2.pdf", f, "application/pdf")})
    assert r.status_code == 200 and r.json()["created"] is False
    assert [v["version"] for v in client.get("/documents/versions").json()] == [1, 2]


def test_document_name_is_a_query_parameter_and_isolated(client):
    # Regression: as a form field, Swagger UI pre-filled it with "string" and created a
    # document literally named "string". Now it is a query parameter.
    with open(V1, "rb") as f:
        r = client.post("/documents/ingest", params={"document": "other manual"},
                        files={"file": ("ct200_manual.pdf", f, "application/pdf")})
    assert r.status_code == 201 and r.json()["version"]["version"] == 1
    assert [v["version"] for v in client.get("/documents/versions", params={"document": "other manual"}).json()] == [1]
    assert [v["version"] for v in client.get("/documents/versions").json()] == [1, 2]   # default untouched
    schema = client.get("/openapi.json").json()["paths"]["/documents/ingest"]["post"]
    assert any(p["name"] == "document" and p["in"] == "query" for p in schema["parameters"])


def test_non_pdf_upload_is_rejected(client):
    r = client.post("/documents/ingest", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 422


def test_version_diff_summary_exposed(client):
    v2 = client.get("/documents/versions/2").json()
    assert v2["diff_summary"]["counts"] == {"unchanged": 24, "modified": 4, "added": 1, "removed": 0}
    assert v2["source_filename"] == "ct200_manual_v2.pdf"


# ---- browse -------------------------------------------------------------------------------

def test_top_level_sections_default_to_latest_and_accept_version(client):
    latest = client.get("/sections").json()
    v1 = client.get("/sections", params={"version": 1}).json()
    assert [n["number"] for n in latest] == [str(i) for i in range(1, 9)]
    assert {n["version"] for n in latest} == {2} and {n["version"] for n in v1} == {1}
    assert client.get("/sections", params={"version": 9}).status_code == 404


def test_node_detail_has_text_hash_parent_path_and_children(client):
    d = client.get(f"/nodes/{node_id(client, '2.1', 1)}").json()
    assert d["parent"]["number"] == "2"
    assert [c["number"] for c in d["children"]] == ["2.1.1.1"]
    assert d["blocks"][0]["kind"] == "table" and len(d["content_hash"]) == 64
    leaf = client.get(f"/nodes/{node_id(client, '2.1.1.1', 1)}").json()
    assert "missing_parent:2.1.1" in leaf["flags"]
    assert [p["number"] for p in leaf["path"]] == [None, "2", "2.1"]


def test_tree_keeps_document_order_for_out_of_order_sections(client):
    tree = client.get("/tree", params={"version": 1}).json()
    ch3 = next(c for c in tree["children"] if c["number"] == "3")
    assert [c["number"] for c in ch3["children"]] == ["3.1", "3.2", "3.4", "3.3"]


def test_search_title_body_and_flag(client):
    titles = client.get("/search", params={"q": "error codes", "field": "title"}).json()
    assert [h["node"]["number"] for h in titles["hits"]] == ["4.2", "7.1"]
    body = client.get("/search", params={"q": "mmHg increments", "version": 1}).json()
    assert body["hits"][0]["node"]["number"] == "3.2" and "40 mmHg increments" in body["hits"][0]["snippet"]
    flagged = client.get("/search", params={"flag": "out_of_order"}).json()
    assert [h["node"]["number"] for h in flagged["hits"]] == ["3.3"]
    assert client.get("/search").status_code == 422                       # no q and no flag
    assert client.get("/search", params={"q": "x", "field": "nope"}).status_code == 422


# ---- change detection ---------------------------------------------------------------------

def test_changed_section_reports_diff_to_latest(client):
    v1_id, v2_id = node_id(client, "4.2", 1), node_id(client, "4.2", 2)
    r = client.get(f"/nodes/{v1_id}/changes").json()
    assert r["status_vs_latest"] == "changed" and r["changed"] is True
    assert r["latest_node_id"] == v2_id
    assert r["diff_to_latest"]["numbers_removed"] == ["2"]
    assert "1.5" in r["diff_to_latest"]["numbers_added"]
    assert [h["version"] for h in r["history"]] == [1, 2]


def test_unchanged_section_and_latest_and_new_section(client):
    r = client.get(f"/nodes/{node_id(client, '7.1', 1)}/changes").json()
    assert r["status_vs_latest"] == "unchanged" and r["diff_to_latest"] is None
    r = client.get(f"/nodes/{node_id(client, '4.2', 2)}/changes").json()
    assert r["status_vs_latest"] == "is_latest"
    r = client.get(f"/nodes/{node_id(client, '5.3', 2)}/changes").json()
    assert [(h["version"], h["change_status"]) for h in r["history"]] == [(2, "added")]


# ---- selections ---------------------------------------------------------------------------

def test_selection_is_pinned_to_v1_text_after_v2_exists(client):
    a, b = node_id(client, "3.2", 1), node_id(client, "4.2", 1)
    r = client.post("/selections", json={"name": "pinned-v1", "node_ids": [b, a, a]})
    assert r.status_code == 201, r.text
    sel = r.json()
    assert sel["pinned_version"] == 1 and sel["latest_version"] == 2
    assert [i["number"] for i in sel["items"]] == ["3.2", "4.2"]          # document order, deduplicated
    assert all(i["status_in_latest"] == "changed" for i in sel["items"])
    again = client.get(f"/selections/{sel['id']}").json()
    assert "40 mmHg increments" in again["text"]                          # v1 text, not v2
    assert "within 2 seconds" in again["text"] and "1.5 seconds" not in again["text"]
    assert again["fingerprint"] == sel["fingerprint"]


def test_selection_expands_chapter_to_descendants(client):
    r = client.post("/selections", json={"name": "chapter-4", "node_ids": [node_id(client, "4", 2)],
                                         "include_descendants": True}).json()
    assert [(i["number"], i["explicit"]) for i in r["items"]] == [
        ("4", True), ("4.1", False), ("4.2", False), ("4.3", False)]


@pytest.mark.parametrize("payload_fn,code", [
    (lambda c: {"name": "mixed", "node_ids": [node_id(c, "3.2", 1), node_id(c, "3.2", 2)]}, 422),
    (lambda c: {"name": "ghost", "node_ids": [node_id(c, "3.2", 1), 999999]}, 404),
    (lambda c: {"name": "empty", "node_ids": []}, 422),
    (lambda c: {"name": "pinned-v1", "node_ids": [node_id(c, "1.1", 1)]}, 409),
])
def test_selection_rejections(client, payload_fn, code):
    client.post("/selections", json={"name": "pinned-v1", "node_ids": [node_id(client, "3.2", 1)]})
    assert client.post("/selections", json=payload_fn(client)).status_code == code


def test_unknown_selection_is_404(client):
    assert client.get("/selections/424242").status_code == 404
