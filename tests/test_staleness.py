"""Staleness: the verdict rules (pure), then the real v1 -> v2 -> v3 history."""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db.base import init_db, make_engine
from app.db.models import DocumentVersion, NodeVersion
from app.generation.service import generate_for_selection
from app.llm.client import ScriptedLLM
from app.services.ingestion import ingest_pdf
from app.services.selections import create_selection
from app.staleness.assess import assess_generation, classify_test_case
from app.storage.generations import InMemoryGenerationStore
from app.versioning.diff import word_diff

from .conftest import V1, V2


# ---- verdict rules (pure function) --------------------------------------------------------

def cited(old, new, number="9.9", status=None):
    d = word_diff(old, new)
    return {"status": status or ("unchanged" if old == new else "changed"), "number": number,
            "current_text": new, "numbers_removed": d["numbers_removed"],
            "numbers_added": d["numbers_added"], "changes": d["changes"]}


OLD = "If the cuff exceeds 300 mmHg the valve opens within 2 seconds. Users should wipe the cuff gently."


def test_unchanged_source_is_current():
    v = classify_test_case("the valve opens within 2 seconds", "opens within 2 seconds", [cited(OLD, OLD)])
    assert (v["status"], v["severity"]) == ("current", "none")


def test_changed_threshold_in_the_quote_is_stale_high():
    new = OLD.replace("within 2 seconds", "within 1.5 seconds")
    v = classify_test_case("the valve opens within 2 seconds", "valve open within 2 seconds", [cited(OLD, new)])
    assert (v["status"], v["severity"]) == ("stale", "high")
    assert "2 -> 1.5" in v["reasons"][1]


def test_one_word_wording_change_in_the_quote_is_stale_but_only_medium():
    # The assignment's question: is a one-word wording change treated like a changed threshold?
    # No: both mean the anchored text changed (stale), but only the threshold change is high.
    new = OLD.replace("the valve opens", "the valve releases")
    v = classify_test_case("the valve opens within 2 seconds", "valve open within 2 seconds", [cited(OLD, new)])
    assert (v["status"], v["severity"]) == ("stale", "medium")


def test_change_elsewhere_in_the_section_only_asks_for_review():
    new = OLD.replace("wipe the cuff gently", "wipe the cuff with a dry cloth")
    v = classify_test_case("the valve opens within 2 seconds", "valve open within 2 seconds", [cited(OLD, new)])
    assert (v["status"], v["severity"], v["quote_still_present"]) == ("review", "low", True)


def test_quote_intact_but_a_value_used_in_the_expected_result_changed_is_review_medium():
    new = OLD.replace("300 mmHg", "299 mmHg")
    v = classify_test_case("the valve opens within 2 seconds", "valve opens at 300 mmHg within 2 seconds",
                           [cited(OLD, new)])
    assert (v["status"], v["severity"]) == ("review", "medium")


def test_removed_source_is_stale_high():
    v = classify_test_case("anything at all here", "x", [{"status": "removed", "number": "5.3", "current_text": None,
                                                          "numbers_removed": [], "numbers_added": [], "changes": []}])
    assert (v["status"], v["severity"]) == ("stale", "high")


# ---- real history: v1 -> v2 -> v3 (v3 is v1 again, so 5.3 disappears) ---------------------

@pytest.fixture(scope="module")
def world(tmp_path_factory):
    engine = make_engine(f"sqlite:///{tmp_path_factory.mktemp('stale') / 's.db'}")
    init_db(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    store = InMemoryGenerationStore()
    ingest_pdf(s, V1)

    def nv(number, version):
        return s.scalars(select(NodeVersion).join(DocumentVersion).where(
            NodeVersion.number == number, DocumentVersion.version_number == version)).one()

    def tc(title, ids, quote, expected):
        return {"title": title, "type": "boundary", "priority": "high", "source_node_ids": ids,
                "requirement_quote": quote, "preconditions": [], "steps": ["run the check"],
                "expected_result": expected}

    def generate(name, numbers, version, cases):
        sel = create_selection(s, name, [nv(n, version).id for n in numbers])
        doc, _ = generate_for_selection(s, store, ScriptedLLM([json.dumps({"test_cases": cases})]), sel.id)
        assert doc["status"] == "succeeded", doc["attempts"]
        return doc

    a, b = nv("3.2", 1).id, nv("4.2", 1).id
    # the four test cases Groq actually produced for 3.2 + 4.2 on the user's machine
    g_main = generate("main", ["3.2", "4.2"], 1, [
        tc("Initial cuff inflation reaches 180 mmHg target", [a],
           "inflates the cuff to an initial target of 180 mmHg", "cuff reaches 180 mmHg"),
        tc("Incremental inflation to max 299 mmHg", [a],
           "inflates in 40 mmHg increments up to a maximum of 299 mmHg", "steps of 40 mmHg, never above 299 mmHg"),
        tc("E3 triggers auto-deflation within 2 seconds", [b],
           "Auto-deflates within 2 seconds, displays E3", "deflated within 2 seconds, E3 shown"),
        tc("E1 aborts measurement and displays code", [b],
           "Aborts measurement, displays E1", "measurement aborted, E1 shown"),
    ])
    c = nv("7.1", 1).id
    g_untouched = generate("untouched", ["7.1"], 1, [
        tc(f"Persistent code escalation {i}", [c], "contact CardioTrack support", "user told to contact support")
        for i in range(3)])
    d = nv("4.3", 1).id
    g_alarms = generate("alarms", ["4.3"], 1, [
        tc(f"Audible alarm scope {i}", [d], "audible alarms are limited to the E1–E5 error conditions",
           "alarms only for E1 to E5") for i in range(3)])
    before = {k: assess_generation(s, g) for k, g in
              {"main": g_main, "untouched": g_untouched, "alarms": g_alarms}.items()}

    ingest_pdf(s, V2)
    e = nv("5.3", 2).id
    g_export = generate("export", ["5.3"], 2, [
        tc(f"CSV export requirement {i}", [e], "exporting stored readings as a CSV file", "CSV produced")
        for i in range(3)])
    g_v2_main = generate("main-v2", ["3.2"], 2, [
        tc(f"Increment check {i}", [nv("3.2", 2).id], "inflates in 30 mmHg increments", "30 mmHg steps")
        for i in range(3)])
    after_v2 = {k: assess_generation(s, g) for k, g in
                {"main": g_main, "untouched": g_untouched, "alarms": g_alarms,
                 "export": g_export, "v2_main": g_v2_main}.items()}

    ingest_pdf(s, V1)                                         # v3 = v1 again: 5.3 removed
    after_v3 = {"export": assess_generation(s, g_export), "main": assess_generation(s, g_main)}
    yield before, after_v2, after_v3
    s.close()
    engine.dispose()


def by_id(report):
    return {c["id"]: (c["status"], c["severity"]) for c in report["test_cases"]}


def test_everything_is_current_before_the_manual_changes(world):
    before, _, _ = world
    assert all(r["status"] == "current" and r["severity"] == "none" for r in before.values())
    assert before["main"]["summary"] == "generated from the latest version (v1)"


def test_real_v2_change_separates_broken_tests_from_intact_ones(world):
    _, after, _ = world
    r = after["main"]
    assert by_id(r) == {
        "TC-1": ("review", "low"),     # 180 mmHg target: still in the manual
        "TC-2": ("stale", "high"),     # 40 mmHg increments: now 30
        "TC-3": ("stale", "high"),     # E3 within 2 s: now 1.5 s
        "TC-4": ("review", "low"),     # E1 row untouched
    }
    assert (r["status"], r["severity"]) == ("stale", "high")
    assert r["test_case_counts"] == {"current": 0, "review": 2, "stale": 2}
    reasons = {c["id"]: " ".join(c["reasons"]) for c in r["test_cases"]}
    assert "40 -> 30" in reasons["TC-2"] and "2 -> 1.5" in reasons["TC-3"]
    assert {s["number"]: s["status"] for s in r["sources"]} == {"3.2": "changed", "4.2": "changed"}


def test_unchanged_section_stays_current_across_versions(world):
    _, after, _ = world
    r = after["untouched"]
    assert r["status"] == "current" and r["checked_against_version"] == 2
    assert r["summary"] == "generated from v1; nothing it cites changed up to v2"


def test_error_code_range_change_is_stale_high(world):
    _, after, _ = world
    assert set(by_id(after["alarms"]).values()) == {("stale", "high")}       # E1–E5 -> E1–E6


def test_generation_made_on_the_latest_version_is_current(world):
    _, after, _ = world
    assert after["v2_main"]["status"] == "current" and after["export"]["status"] == "current"


def test_removed_section_makes_its_test_cases_stale_high(world):
    _, _, v3 = world
    r = v3["export"]
    assert (r["status"], r["severity"]) == ("stale", "high")
    assert r["sources"][0]["status"] == "removed" and r["sources"][0]["latest_node_id"] is None


def test_reverting_the_manual_makes_old_tests_current_again(world):
    _, _, v3 = world
    # v3 is byte-identical to v1, so test cases generated from v1 are valid again
    assert v3["main"]["status"] == "current" and v3["main"]["checked_against_version"] == 3
