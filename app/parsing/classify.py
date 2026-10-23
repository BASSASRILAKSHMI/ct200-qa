"""Stage 2: ordered Lines/Tables -> structural elements (title, headings, blocks).

Rules here come straight from docs/pdf_findings.md:
* A heading is decided by NUMBERING + BOLD + LEFT MARGIN, never by font size alone
  (3.2 is body-sized; table headers are bold at body size).
* Indented, regular-weight "1. Normal: ..." lines are list items, not chapters.
* Paragraphs continue across page breaks when the sentence is unfinished.
* Every line ends up in exactly one element; leftovers are reported, never dropped.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional, Union

from .models import Block, Line, Table

HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
LIST_ITEM_RE = re.compile(r"^(?:\(?\d{1,2}[.)]|\(?[a-z][.)]|[•▪◦*\-–])\s+\S")
SENTENCE_END_RE = re.compile(r"[.!?:]['\")\]]?$")

MARGIN_TOL = 3.0   # pt: how far from the body margin a line may start and still be "at the margin"
INDENT_MIN = 5.0   # pt: minimum indent for a list item


@dataclass
class TitleEl:
    text: str
    page: int
    line_ids: list[int]


@dataclass
class HeadingEl:
    number: Optional[str]
    title: str
    page: int
    size: float
    line_ids: list[int]
    flags: list[str] = field(default_factory=list)


@dataclass
class BlockEl:
    block: Block
    line_ids: list[int]


Element = Union[TitleEl, HeadingEl, BlockEl]


@dataclass
class Layout:
    """Document-level measurements learned from the PDF itself (not hard-coded)."""

    margin_x: float
    body_size: float
    title_size: float
    right_x: float      # typical right edge of a full body line

    def is_full_line(self, line: Line) -> bool:
        """Line runs (almost) to the right edge, i.e. it was wrapped, not the end of a paragraph."""
        width = self.right_x - self.margin_x
        return line.x1 >= self.right_x - FULL_LINE_SLACK * width


FULL_LINE_SLACK = 0.08  # a line ending within 8% of the text width from the right edge is "full"


def learn_layout(lines: list[Line]) -> Layout:
    body = [l for l in lines if l.bold is not True]
    margin = Counter(round(l.x0) for l in body).most_common(1)[0][0] if body else 0
    size = Counter(l.size for l in body).most_common(1)[0][0] if body else 11.0
    first_page = [l for l in lines if l.page == min(x.page for x in lines)]
    title_size = max((l.size for l in first_page), default=size)
    rights = sorted(l.x1 for l in body if abs(l.x0 - margin) <= MARGIN_TOL)
    right = rights[int(0.9 * (len(rights) - 1))] if rights else margin + 300  # 90th percentile, robust
    return Layout(margin_x=float(margin), body_size=size, title_size=title_size, right_x=right)


def _at_margin(line: Line, lay: Layout) -> bool:
    return abs(line.x0 - lay.margin_x) <= MARGIN_TOL


def _is_heading(line: Line, lay: Layout) -> Optional[re.Match]:
    if not _at_margin(line, lay):
        return None
    m = HEADING_RE.match(line.text)
    if not m or not m.group(2)[0].isupper():
        return None
    if line.bold is True:
        return m
    if line.bold is None:  # OCR: no weight info -> stricter textual test
        title = m.group(2)
        if len(title) <= 90 and not SENTENCE_END_RE.search(title):
            return m
    return None


def classify(items: list[Union[Line, Table]], lines: list[Line]) -> tuple[list[Element], set[int], Layout]:
    lay = learn_layout(lines)
    elements: list[Element] = []
    consumed: set[int] = set()

    para: Optional[BlockEl] = None      # open paragraph
    lst: Optional[BlockEl] = None       # open list
    prev_line: Optional[Line] = None
    title_done = False

    def close() -> None:
        nonlocal para, lst
        para = lst = None

    i = 0
    while i < len(items):
        it = items[i]

        # ---- tables -------------------------------------------------------
        if isinstance(it, Table):
            close()
            blk = Block(kind="table", page=it.page, header=it.header, rows=it.rows)
            elements.append(BlockEl(blk, list(it.line_ids)))
            consumed.update(it.line_ids)
            prev_line = None
            i += 1
            continue

        line: Line = it

        # ---- document title (multi-line, page 1, largest font) -------------
        if not title_done and line.page == 1 and line.size >= lay.title_size - 0.5 and lay.title_size > lay.body_size + 2:
            ids, parts = [line.id], [line.text]
            j = i + 1
            while j < len(items) and isinstance(items[j], Line) and items[j].size >= lay.title_size - 0.5:
                ids.append(items[j].id)
                parts.append(items[j].text)
                j += 1
            elements.append(TitleEl(" ".join(parts), line.page, ids))
            consumed.update(ids)
            title_done = True
            prev_line = items[j - 1]
            i = j
            continue
        title_done = True

        # ---- numbered heading ---------------------------------------------
        m = _is_heading(line, lay)
        if m:
            close()
            ids, title = [line.id], m.group(2)
            # heading wrapped onto a second line: same weight/size, at margin, not numbered, tight gap
            j = i + 1
            while (
                j < len(items)
                and isinstance(items[j], Line)
                and line.bold is True
                and items[j].bold is True
                and items[j].page == line.page
                and abs(items[j].size - line.size) < 0.2
                and _at_margin(items[j], lay)
                and not HEADING_RE.match(items[j].text)
                and items[j].y0 - items[j - 1].y1 < 0.5 * line.size
            ):
                title += " " + items[j].text
                ids.append(items[j].id)
                j += 1
            elements.append(HeadingEl(m.group(1), title, line.page, line.size, ids))
            consumed.update(ids)
            prev_line = items[j - 1]
            i = j
            continue

        # ---- bold line at margin but no number: unnumbered heading ---------
        if line.bold is True and _at_margin(line, lay) and len(line.text) <= 90:
            close()
            elements.append(HeadingEl(None, line.text, line.page, line.size, [line.id], ["unnumbered_heading"]))
            consumed.add(line.id)
            prev_line = line
            i += 1
            continue

        indented = line.x0 >= lay.margin_x + INDENT_MIN

        # ---- list items and their wrapped continuation lines ---------------
        if indented and LIST_ITEM_RE.match(line.text):
            para = None
            if lst is None:
                lst = BlockEl(Block(kind="list", page=line.page), [])
                elements.append(lst)
            lst.block.items.append(line.text)
            lst.line_ids.append(line.id)
            consumed.add(line.id)
            prev_line = line
            i += 1
            continue
        if lst is not None and indented:
            lst.block.items[-1] += " " + line.text
            lst.line_ids.append(line.id)
            consumed.add(line.id)
            prev_line = line
            i += 1
            continue

        # ---- paragraph lines ----------------------------------------------
        lst = None
        if para is not None and prev_line is not None:
            if prev_line.page == line.page:
                new_para = (line.y0 - prev_line.y1) > 0.5 * lay.body_size
            else:
                # Page break. Continue the paragraph if the sentence is unfinished OR the last
                # line on the previous page was a full (wrapped) line. A sentence can end exactly
                # at the bottom of a page (3.1 in v1), so punctuation alone is not enough.
                unfinished = not SENTENCE_END_RE.search(para.block.text)
                new_para = not (unfinished or lay.is_full_line(prev_line))
            if new_para:
                para = None
        if para is None:
            para = BlockEl(Block(kind="paragraph", page=line.page, text=line.text), [line.id])
            elements.append(para)
        else:
            para.block.text += " " + line.text
            para.line_ids.append(line.id)
        consumed.add(line.id)
        prev_line = line
        i += 1

    return elements, consumed, lay
