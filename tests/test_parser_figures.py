"""Figures: the CT-200 PDFs contain none, but the assignment lists figures and captions, and a
parser must not silently drop content. Tested on a small PDF built here with real images."""
import pymupdf
import pytest

from app.parsing.parser import parse_pdf


@pytest.fixture()
def pdf_with_figures(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=420, height=595)
    y = 60

    def text(s, size=11, bold=False, dy=16):
        nonlocal y
        page.insert_text((33, y), s, fontsize=size, fontname="hebo" if bold else "helv")
        y += dy

    def image(h=60):
        nonlocal y
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 50), False)
        pix.clear_with(180)
        page.insert_image(pymupdf.Rect(33, y, 153, y + h), pixmap=pix)
        y += h + 14

    text("Test Device Manual", size=22, bold=True, dy=50)
    text("1. Cuff Placement", size=16.5, bold=True, dy=26)
    text("Wrap the cuff around the upper arm as shown below.")
    image()
    text("Figure 1: Correct cuff position on the left arm")
    text("Keep the tube aligned with the inner arm.", dy=40)
    text("2. Display", size=16.5, bold=True, dy=26)
    image()                                                       # no caption
    text("The display shows systolic and diastolic values.")
    path = tmp_path / "figures.pdf"
    doc.save(path)
    return str(path)


def test_figure_is_kept_in_its_section_with_its_caption(pdf_with_figures):
    r = parse_pdf(pdf_with_figures)
    s1 = r.by_number("1")[0]
    assert [b.kind for b in s1.blocks] == ["paragraph", "figure", "paragraph"]
    assert s1.blocks[1].text == "Figure 1: Correct cuff position on the left arm"
    assert "Figure 1" not in s1.blocks[2].text                 # caption not also a paragraph
    assert r.unassigned_lines == [] and r.duplicated_line_ids == []


def test_uncaptioned_figure_is_still_recorded_and_reported(pdf_with_figures):
    r = parse_pdf(pdf_with_figures)
    s2 = r.by_number("2")[0]
    assert [b.kind for b in s2.blocks] == ["figure", "paragraph"]
    assert "[Figure]" in s2.body                               # figures count toward the hash
    figure_warnings = [w for w in r.warnings if w.startswith("figure on page")]
    assert len(figure_warnings) == 2
    assert any("no caption found" in w for w in figure_warnings)


def test_ct200_manuals_have_no_figures(v1, v2):
    for r in (v1, v2):
        assert not [b for n in r.nodes.values() for b in n.blocks if b.kind == "figure"]
