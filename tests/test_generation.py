"""Generation service: every way an LLM reply can go wrong, and the duplicate policy.

The LLM is a ScriptedLLM: each test lists exactly what the "model" replies, in order.
If the service calls the LLM more often than the script allows, the test fails, which is
how "no LLM call happened" is asserted.
"""
import json

import mongomock
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import config
from app.db.base import init_db, make_engine
from app.db.models import DocumentVersion, NodeVersion
from app.generation.service import generate_for_selection
from app.llm.client import LLMConfigError, LLMResponse, LLMUnavailable, ScriptedLLM
from app.services.ingestion import ingest_pdf
from app.services.queries import BadRequest
from app.services.selections import create_selection
from app.storage.generations import InMemoryGenerationStore, MongoGenerationStore

from .conftest import V1, V2


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    engine = make_engine(f"sqlite:///{tmp_path_factory.mktemp('gen') / 'gen.db'}")
    init_db(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    ingest_pdf(s, V1)
    ingest_pdf(s, V2)
    yield s
    s.close()
    engine.dispose()


def node(db, number, version=1):
    return db.scalars(select(NodeVersion).join(DocumentVersion).where(
        NodeVersion.number == number, DocumentVersion.version_number == version)).one()


_counter = iter(range(10_000))


@pytest.fixture()
def selection(db):
    return create_selection(db, f"sel-{next(_counter)}", [node(db, "3.2").id, node(db, "4.2").id])


def case(title, ids, quote, **kw):
    d = {"title": title, "type": "boundary", "priority": "high", "source_node_ids": ids,
         "requirement_quote": quote, "preconditions": ["cuff fitted on an adult arm"],
         "steps": ["start a measurement", "observe the display"], "expected_result": "matches the manual"}
    d.update(kw)
    return d


def good_reply(db, extra=None):
    a, b = node(db, "3.2").id, node(db, "4.2").id
    cases = [
        case("Initial inflation target is 180 mmHg", [a], "inflates the cuff to an initial target of 180 mmHg"),
        case("Inflation increments of 40 mmHg", [a], "inflates in 40 mmHg increments up to a maximum of 299 mmHg"),
        case("E3 overpressure deflates within 2 seconds", [b], "Auto-deflates within 2 seconds, displays E3",
             type="safety"),
    ] + (extra or [])
    return json.dumps({"test_cases": cases})


def run(db, sel, script, store=None, force=False, model="scripted-model"):
    store = store if store is not None else InMemoryGenerationStore()
    llm = ScriptedLLM(script, model=model)
    doc, reused = generate_for_selection(db, store, llm, sel.id, force=force)
    return doc, reused, llm, store


# ---- happy path ---------------------------------------------------------------------------

def test_valid_reply_is_stored_with_traceable_sources(db, selection):
    doc, reused, llm, store = run(db, selection, [good_reply(db)])
    assert doc["status"] == "succeeded" and reused is False
    assert [t["id"] for t in doc["test_cases"]] == ["TC-1", "TC-2", "TC-3"]
    assert {s["number"]: s["content_hash"] for s in doc["sources"]} == {
        "3.2": node(db, "3.2").content_hash, "4.2": node(db, "4.2").content_hash}
    assert {s["version"] for s in doc["sources"]} == {1}
    assert store.get(doc["_id"])["status"] == "succeeded"
    assert len(doc["attempts"]) == 1 and doc["attempts"][0]["raw"]


def test_prompt_contains_pinned_v1_text_and_node_ids(db, selection):
    _, _, llm, _ = run(db, selection, [good_reply(db)])
    user = llm.calls[0][1]["content"]
    assert f'node_id="{node(db, "3.2").id}"' in user
    assert "40 mmHg increments" in user and "30 mmHg increments" not in user   # v1, not v2
    assert "JSON" in llm.calls[0][0]["content"]                               # needed for Groq JSON mode


def test_markdown_fenced_json_is_accepted_and_the_repair_recorded(db, selection):
    doc, *_ = run(db, selection, ["```json\n" + good_reply(db) + "\n```"])
    assert doc["status"] == "succeeded" and doc["attempts"][0]["repaired"] == "stripped markdown code fence"


# ---- malformed replies: retried with feedback ---------------------------------------------

@pytest.mark.parametrize("bad,expected_error", [
    ("Sure! Here are some great test cases for you.", "not valid JSON"),
    ('{"test_cases": [{"title": "Initial infla', "not valid JSON"),                 # truncated
    ("", "empty"),
    ('[{"title": "x"}]', "top level must be a JSON object"),
    ('{"cases": []}', "test_cases"),                                                  # wrong key
])
def test_unparseable_or_wrong_shape_reply_is_retried_with_the_error(db, selection, bad, expected_error):
    doc, _, llm, _ = run(db, selection, [bad, good_reply(db)])
    assert doc["status"] == "succeeded" and len(doc["attempts"]) == 2
    assert any(expected_error in e for e in doc["attempts"][0]["errors"])
    feedback = llm.calls[1][-1]["content"]
    assert "rejected" in feedback and expected_error.split()[0] in feedback


def test_reply_cut_off_by_length_limit_is_retried(db, selection):
    cut = LLMResponse(text=good_reply(db)[:150], model="m", finish_reason="length")
    doc, *_ = run(db, selection, [cut, good_reply(db)])
    assert "cut off" in doc["attempts"][0]["errors"][0] and doc["status"] == "succeeded"


def test_too_few_and_too_many_cases_are_not_trimmed_or_padded(db, selection):
    a = node(db, "3.2").id
    two = json.loads(good_reply(db))["test_cases"][:2]
    six = json.loads(good_reply(db, [case(f"Extra case number {i}", [a], "inflates in 40 mmHg increments")
                                     for i in range(3)]))["test_cases"]
    doc, *_ = run(db, selection, [json.dumps({"test_cases": two}), json.dumps({"test_cases": six}), good_reply(db)])
    assert "got 2" in doc["attempts"][0]["errors"][0] and "got 6" in doc["attempts"][1]["errors"][0]
    assert len(doc["test_cases"]) == 3


def test_case_citing_a_node_that_was_not_sent_is_rejected(db, selection):
    foreign = node(db, "7.1").id
    bad = good_reply(db, [case("Support contact on persistent code", [foreign], "contact CardioTrack support")])
    doc, *_ = run(db, selection, [bad, good_reply(db)])
    assert f"[{foreign}]" in doc["attempts"][0]["errors"][0] and "not provided" in doc["attempts"][0]["errors"][0]


def test_hallucinated_requirement_is_rejected_even_though_the_json_is_valid(db, selection):
    b = node(db, "4.2").id
    bad = json.dumps({"test_cases": json.loads(good_reply(db))["test_cases"][:2] + [
        case("E3 deflates within half a second", [b], "Auto-deflates within 0.5 seconds")]})
    doc, *_ = run(db, selection, [bad, good_reply(db)])
    assert "not copied word for word" in doc["attempts"][0]["errors"][0]


def test_quote_from_a_different_section_than_cited_is_rejected(db, selection):
    a = node(db, "3.2").id
    bad = good_reply(db, [case("Wrong citation", [a], "Auto-deflates within 2 seconds, displays E3")])
    doc, *_ = run(db, selection, [bad, good_reply(db)])
    assert "not copied word for word" in doc["attempts"][0]["errors"][0]


def test_schema_violations_are_reported_per_field(db, selection):
    cases = json.loads(good_reply(db))["test_cases"]
    cases[0]["type"] = "smoke"
    cases[1]["steps"] = []
    del cases[2]["expected_result"]
    doc, *_ = run(db, selection, [json.dumps({"test_cases": cases}), good_reply(db)])
    errs = " ".join(doc["attempts"][0]["errors"])
    assert "test_cases.0.type" in errs and "test_cases.1.steps" in errs and "test_cases.2.expected_result" in errs


def test_extra_fields_from_the_model_are_ignored(db, selection):
    cases = json.loads(good_reply(db))["test_cases"]
    cases[0]["id"] = "whatever"
    cases[0]["confidence"] = 0.9
    doc, *_ = run(db, selection, [json.dumps({"test_cases": cases, "note": "hi"})])
    assert doc["status"] == "succeeded" and "confidence" not in doc["test_cases"][0]


# ---- giving up ----------------------------------------------------------------------------

def test_three_bad_replies_store_a_failed_generation_with_every_attempt(db, selection):
    doc, reused, _, store = run(db, selection, ["nope", "still nope", '{"test_cases": []}'])
    assert doc["status"] == "failed" and doc["failure_reason"] == "invalid_output"
    assert doc["test_cases"] == [] and len(doc["attempts"]) == config.LLM_MAX_CONTENT_ATTEMPTS
    assert [a["raw"] for a in doc["attempts"]] == ["nope", "still nope", '{"test_cases": []}']
    assert store.get(doc["_id"])["status"] == "failed"


def test_provider_unavailable_and_bad_key_are_recorded_as_failures(db, selection):
    doc, *_ = run(db, selection, [LLMUnavailable("rate limited 4 times")])
    assert doc["status"] == "failed" and doc["failure_reason"] == "llm_unavailable" and doc["attempts"] == []
    doc, *_ = run(db, selection, [LLMConfigError("HTTP 401")])
    assert doc["failure_reason"] == "llm_config_error" and "401" in doc["failure_detail"]


def test_selection_with_only_empty_headings_is_refused_before_calling_the_llm(db):
    sel = create_selection(db, f"headings-{next(_counter)}", [node(db, "4").id])
    with pytest.raises(BadRequest, match="only headings"):
        run(db, sel, [])                                  # empty script: any call would fail


# ---- duplicate-submission policy ----------------------------------------------------------

def test_same_selection_twice_returns_stored_result_without_calling_llm(db, selection):
    store = InMemoryGenerationStore()
    first, *_ = run(db, selection, [good_reply(db)], store=store)
    second, reused, llm, _ = run(db, selection, [], store=store)       # empty script: no call allowed
    assert reused is True and second["_id"] == first["_id"] and llm.calls == []


def test_force_regenerates_and_links_to_the_superseded_result(db, selection):
    store = InMemoryGenerationStore()
    first, *_ = run(db, selection, [good_reply(db)], store=store)
    second, reused, *_ = run(db, selection, [good_reply(db)], store=store, force=True)
    assert reused is False and second["supersedes"] == first["_id"] and second["forced"]
    assert len(store.find(selection_id=selection.id)) == 2             # old result kept


def test_failed_generations_are_never_reused(db, selection):
    store = InMemoryGenerationStore()
    failed, *_ = run(db, selection, ["x", "y", "z"], store=store)
    ok, reused, *_ = run(db, selection, [good_reply(db)], store=store)
    assert failed["status"] == "failed" and reused is False and ok["status"] == "succeeded"


def test_different_model_is_a_different_request(db, selection):
    store = InMemoryGenerationStore()
    run(db, selection, [good_reply(db)], store=store, model="model-a")
    _, reused, *_ = run(db, selection, [good_reply(db)], store=store, model="model-b")
    assert reused is False


def test_same_nodes_in_a_new_selection_reuse_the_result(db, selection):
    # the key is the pinned TEXT (fingerprint), not the selection's name or id
    store = InMemoryGenerationStore()
    first, *_ = run(db, selection, [good_reply(db)], store=store)
    twin = create_selection(db, f"twin-{next(_counter)}", [node(db, "4.2").id, node(db, "3.2").id])
    doc, reused, *_ = run(db, twin, [], store=store)
    assert reused is True and doc["_id"] == first["_id"]


# ---- coverage warnings and storage backends -----------------------------------------------

def test_selected_section_without_any_test_case_is_warned_not_rejected(db):
    sel = create_selection(db, f"three-{next(_counter)}",
                           [node(db, "3.2").id, node(db, "4.2").id, node(db, "4.3").id])
    doc, *_ = run(db, sel, [good_reply(db)])
    assert doc["status"] == "succeeded"
    assert any("4.3" in w for w in doc["warnings"])


def test_works_identically_with_the_mongodb_store(db, selection):
    store = MongoGenerationStore(client=mongomock.MongoClient())
    doc, *_ = run(db, selection, [good_reply(db)], store=store)
    assert store.find(node_id=node(db, "4.2").id)[0]["_id"] == doc["_id"]
    _, reused, *_ = run(db, selection, [], store=store)
    assert reused is True
