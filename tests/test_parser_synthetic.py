"""Edge cases that the CT-200 PDFs do not contain, tested with hand-built lines.

These run the same classify -> build_tree code as the real parser, just without a PDF.
"""
from app.parsing.classify import classify
from app.parsing.extract import _merge_split_tables
from app.parsing.models import Line, Table
from app.parsing.normalize import content_hash, normalize_text
from app.parsing.tree import build_tree

MARGIN = 33.0


class LineMaker:
    def __init__(self):
        self.lines = []
        self.y = {}

    def add(self, text, bold=False, size=11.0, x=MARGIN, page=1, gap=13.2, width=350):
        y = self.y.get(page, 30.0) + gap
        self.y[page] = y
        line = Line(id=len(self.lines), page=page, x0=x, y0=y, x1=x + width, y1=y + size,
                    size=size, bold=bold, text=text)
        self.lines.append(line)
        return line


def parse(lines, items=None):
    elements, consumed, _ = classify(items or lines, lines)
    nodes, warnings = build_tree(elements)
    return nodes, warnings, consumed


def test_duplicate_section_number_gets_distinct_key_and_is_flagged():
    m = LineMaker()
    m.add("1. Intro", bold=True, size=16.5)
    m.add("Body one.", gap=30)
    m.add("1. Intro again", bold=True, size=16.5, gap=30)
    m.add("Body two.", gap=30)
    nodes, warnings, _ = parse(m.lines)
    assert "1" in nodes and "1#2" in nodes
    assert "duplicate_number" in nodes["1#2"].flags
    assert nodes["1"].body == "Body one." and nodes["1#2"].body == "Body two."
    assert any("more than once" in w for w in warnings)


def test_unnumbered_bold_heading_is_kept_flagged_and_nested():
    m = LineMaker()
    m.add("1. Safety", bold=True, size=16.5)
    m.add("Warnings", bold=True, gap=30)
    m.add("Do not use near water.", gap=30)
    nodes, warnings, _ = parse(m.lines)
    child = [n for n in nodes.values() if n.title == "Warnings"][0]
    assert child.parent_key == "1"
    assert "unnumbered_heading" in child.flags
    assert child.body == "Do not use near water."
    assert any("unnumbered heading" in w for w in warnings)


def test_ocr_lines_without_font_weight_still_find_headings_but_not_sentences():
    m = LineMaker()
    h = m.add("2. Specifications")
    body = m.add("4 batteries are included in the box.", gap=30)
    h.bold = body.bold = None   # OCR: weight unknown
    nodes, _, _ = parse(m.lines)
    assert "2" in nodes and nodes["2"].title == "Specifications"
    assert "4" not in nodes                      # sentence starting with a number stays body text
    assert "4 batteries" in nodes["2"].body


def test_wrapped_two_line_heading_is_merged():
    m = LineMaker()
    m.add("4.1 A very long heading that the", bold=True, size=12.9)
    m.add("layout wrapped onto a second line", bold=True, size=12.9, gap=14)
    m.add("Body.", gap=30)
    nodes, _, consumed = parse(m.lines)
    assert nodes["4.1"].title == "A very long heading that the layout wrapped onto a second line"
    assert len(consumed) == 3


def test_new_paragraph_on_next_page_when_previous_ended_with_short_line():
    m = LineMaker()
    m.add("1. Topic", bold=True, size=16.5)
    m.add("First paragraph ends here.", gap=30, width=120)   # short last line + full stop
    m.add("Second paragraph on the next page.", page=2)
    nodes, _, _ = parse(m.lines)
    assert [b.kind for b in nodes["1"].blocks] == ["paragraph", "paragraph"]


def test_table_split_across_pages_is_merged_and_flagged():
    t1 = Table(page=1, y0=500, y1=590, header=["Code", "Meaning"], rows=[["E1", "a"]], line_ids=[0, 1])
    t2 = Table(page=2, y0=30, y1=80, header=["Code", "Meaning"], rows=[["E2", "b"]], line_ids=[2, 3])
    items = [t1, t2]
    _merge_split_tables(items)
    assert len(items) == 1
    assert items[0].rows == [["E1", "a"], ["E2", "b"]]       # repeated header dropped
    assert items[0].continued


def test_normalisation_makes_ligatures_and_nb_hyphens_hash_equal():
    assert normalize_text("Cuﬀ Speciﬁcations for CT‑200") == "Cuff Specifications for CT-200"
    assert content_hash("non‑volatile  memory\n") == content_hash("non-volatile memory")
    # an en dash in a numeric range is meaningful and must NOT be folded into a hyphen
    assert content_hash("22–42 cm") != content_hash("22-42 cm")
