"""Parser tests against the real CT-200 PDFs.

Each test targets one irregularity from docs/pdf_findings.md (section number in brackets).
"""
import re
from collections import Counter

import pymupdf
import pytest

from app.parsing.normalize import normalize_text

from .conftest import V1, V2


def node(result, number):
    found = result.by_number(number)
    assert len(found) == 1, f"expected exactly one node numbered {number}, got {len(found)}"
    return found[0]


# [6] duplicate heading titles ----------------------------------------------------------------
def test_duplicate_heading_titles_get_distinct_nodes_and_correct_parents(v1):
    error_codes = v1.by_title("Error Codes")
    assert len(error_codes) == 2
    a, b = error_codes
    assert a.key != b.key
    assert {a.parent_key, b.parent_key} == {"4", "7"}
    assert all("duplicate_title" in n.flags for n in error_codes)
    # content stays with the right one: the table is in 4.2, the support advice in 7.1
    assert node(v1, "4.2").blocks[0].kind == "table"
    assert "contact CardioTrack support" in node(v1, "7.1").body


# [3] skipped hierarchy level -----------------------------------------------------------------
def test_skipped_level_attaches_to_nearest_existing_ancestor_without_phantom_node(v1):
    battery = node(v1, "2.1.1.1")
    assert battery.parent_key == "2.1"
    assert battery.level == 4                       # true depth from numbering is preserved
    assert "missing_parent:2.1.1" in battery.flags
    assert v1.by_number("2.1.1") == []              # no invented 2.1.1 node
    assert "300 measurement cycles" in battery.body


# [4] out-of-order siblings -------------------------------------------------------------------
def test_out_of_order_sections_keep_document_order_and_are_flagged(v1):
    chapter3 = v1.nodes["3"]
    assert [v1.nodes[k].number for k in chapter3.children] == ["3.1", "3.2", "3.4", "3.3"]
    assert any(f.startswith("out_of_order") for f in node(v1, "3.3").flags)
    assert not any(f.startswith("out_of_order") for f in node(v1, "3.4").flags)
    # 3.4's body must not leak into 3.3 or vice versa
    assert "60 seconds of inactivity" in node(v1, "3.4").body
    assert "60 seconds" not in node(v1, "3.3").body


# [5] numbered list that looks like chapter headings ------------------------------------------
def test_numbered_list_inside_3_3_is_a_list_not_top_level_sections(v1):
    top_level = [v1.nodes[k].number for k in v1.root.children]
    assert top_level == ["1", "2", "3", "4", "5", "6", "7", "8"]
    lists = [b for b in node(v1, "3.3").blocks if b.kind == "list"]
    assert len(lists) == 1
    items = lists[0].items
    assert len(items) == 5
    assert items[0].startswith("1. Normal")
    # wrapped continuation line is joined to item 5, not lost or turned into a paragraph
    assert items[4].endswith("recommends seeking immediate medical attention")
    # chapter 4 still parents 4.x (a bogus "4. Hypertension Stage 2" heading would steal them)
    assert node(v1, "4.1").parent_key == "4"


# [1] heading level comes from numbering, not font size ---------------------------------------
def test_body_sized_heading_3_2_is_level_2_sibling_of_3_1(v1):
    s32 = node(v1, "3.2")
    assert s32.level == 2 and s32.parent_key == "3"
    assert "40 mmHg increments" in s32.body


# [2] bold table header cells are not headings ------------------------------------------------
def test_bold_table_headers_are_not_headings(v1):
    titles = {n.title for n in v1.nodes.values()}
    assert not titles & {"Parameter", "Value", "Code", "Meaning", "Device Behavior"}


# [8] tables, including multi-line cells and exact numeric values -----------------------------
def test_error_code_table_cells_are_exact(v1, v2):
    t1 = node(v1, "4.2").blocks[0]
    assert t1.header == ["Code", "Meaning", "Device Behavior"]
    rows1 = {r[0]: r for r in t1.rows}
    assert list(rows1) == ["E1", "E2", "E3", "E4", "E5"]
    assert rows1["E1"][1] == "Cuff not connected or leak detected"       # multi-line cell joined
    assert rows1["E3"][2] == "Auto-deflates within 2 seconds, displays E3"

    rows2 = {r[0]: r for r in node(v2, "4.2").blocks[0].rows}
    assert rows2["E3"][2] == "Auto-deflates within 1.5 seconds, displays E3"  # not "1 .5"
    assert "E6" in rows2


def test_each_table_section_has_exactly_one_table_and_no_line_is_claimed_twice(v1, v2):
    # Regression: PyMuPDF 1.26-1.28 return extra 1-row "tables" nested inside real ones,
    # which duplicated cell text. Found only when the suite ran on another machine.
    for result in (v1, v2):
        for num in ("2.1", "4.2"):
            assert [b.kind for b in node(result, num).blocks] == ["table"], num
        assert result.duplicated_line_ids == []


def test_spec_table_values_are_exact(v1):
    table = node(v1, "2.1").blocks[0]
    values = dict(table.rows)
    assert values["Pressure range"] == "0–299 mmHg"   # not "0 –299"
    assert values["Accuracy (pressure)"] == "±3 mmHg"
    assert len(table.rows) == 7


# [7] page breaks -----------------------------------------------------------------------------
def test_paragraph_split_mid_sentence_across_pages_is_joined(v2):
    body = node(v2, "6.1").body
    assert "and do not use alcohol" in body
    assert len(node(v2, "6.1").blocks) == 1
    assert node(v2, "8.1").body.endswith("oscillometric devices.")   # last 2 lines are on page 7


def test_paragraph_ending_exactly_at_page_bottom_is_still_one_paragraph(v1):
    s31 = node(v1, "3.1")
    assert len(s31.blocks) == 1
    assert "power on the device. Use the profile button" in s31.body


def test_chapter_heading_at_page_bottom_keeps_its_children(v1):
    # "2. Physical and Electrical Specifications" is the last line of page 1
    assert v1.nodes["2"].page == 1
    assert [v1.nodes[k].number for k in v1.nodes["2"].children] == ["2.1", "2.2"]


# [10] numbers inside sentences ---------------------------------------------------------------
def test_inline_section_references_are_not_headings(v1):
    assert "see 2.1, 4.3 for related specifications" in node(v1, "3.3").body
    assert "Section 4.2 appears" in node(v1, "7.1").body


# [11] multi-line title -----------------------------------------------------------------------
def test_multiline_title_becomes_document_root(v1):
    assert v1.root.title == "CardioTrack CT-200 Home Blood Pressure Monitor — Technical & User Manual"


# Coverage: nothing silently dropped ----------------------------------------------------------
_NUM = re.compile(r"^\d+(?:\.\d+)*\.?$")


def _words(text):
    out = []
    for w in normalize_text(text).split():
        if w == "|":                      # table cell separator we add when rendering
            continue
        out.append(w.rstrip(".") if _NUM.match(w) else w)
    return out


@pytest.mark.parametrize("pdf,fixture", [(V1, "v1"), (V2, "v2")])
def test_every_word_in_the_pdf_appears_exactly_once_in_the_tree(pdf, fixture, request):
    result = request.getfixturevalue(fixture)
    assert result.unassigned_lines == []

    raw = Counter()
    for page in pymupdf.open(pdf):
        raw.update(_words(page.get_text()))

    parsed = Counter()
    for n in result.nodes.values():
        parsed.update(_words(n.title))
        if n.number:
            parsed.update(_words(n.number))
        parsed.update(_words(n.body))

    assert raw - parsed == Counter(), "words in PDF missing from tree"
    assert parsed - raw == Counter(), "words in tree that are not in PDF"


# [9] hashes: layout/encoding changes are invisible, content changes are not ------------------
def test_unchanged_sections_hash_identically_across_versions(v1, v2):
    unchanged = ["1", "1.1", "1.2", "2.1", "2.2", "3.1", "3.3", "3.4", "4.1",
                 "5.1", "5.2", "6.1", "6.2", "7.1", "7.2", "8.1"]
    for num in unchanged:
        assert node(v1, num).content_hash == node(v2, num).content_hash, num


def test_changed_sections_hash_differently(v1, v2):
    for num in ["2.1.1.1", "3.2", "4.2", "4.3"]:
        assert node(v1, num).content_hash != node(v2, num).content_hash, num
    assert v1.by_number("5.3") == [] and len(v2.by_number("5.3")) == 1


def test_known_warnings_are_reported(v1):
    joined = "\n".join(v1.warnings)
    assert "2.1.1" in joined and "3.3 appears after 3.4" in joined and "Error Codes" in joined
