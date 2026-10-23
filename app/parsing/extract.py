"""Stage 1: PDF -> ordered stream of Lines and Tables.

Text-layer first, OCR fallback per page:
* Pages with a text layer are read with PyMuPDF (exact characters, font name, size, position).
* Pages without one (scanned) are rasterised and run through Tesseract. OCR lines carry
  `bold=None` because OCR cannot see font weight; the classifier adapts to that.

Tables: PyMuPDF `find_tables()` is used ONLY for geometry (where the cells are). Its own
`extract()` produced corrupted cell text on this document ("1 .5 seconds", "0 –299 mmHg",
commas split onto separate lines), so cell text is rebuilt from our own text lines.
"""
from __future__ import annotations

from typing import Union

import pymupdf

from .models import Figure, Line, Table
from .normalize import clean_display_text

Item = Union[Line, Table, Figure]

OCR_DPI = 300
MIN_TEXT_CHARS = 20  # a page with fewer extractable characters is treated as scanned
MIN_FIGURE_PT = 20   # images smaller than this (bullets, icons) are decoration, not figures


def _figures_for_page(page: pymupdf.Page, pno: int) -> list[Figure]:
    """Placed images on a text-layer page. (On an OCR'd page the whole page is an image.)"""
    out = []
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        if (x1 - x0) >= MIN_FIGURE_PT and (y1 - y0) >= MIN_FIGURE_PT:
            out.append(Figure(page=pno, x0=x0, y0=y0, x1=x1, y1=y1))
    return out


def _is_bold(spans: list[dict]) -> bool:
    """Majority (by character count) of the line's visible text is in a bold font.

    Needed because "CT‑200" is split into spans and the U+2011 hyphen sits in a separate
    regular-weight font even inside bold headings.
    """
    bold = regular = 0
    for s in spans:
        n = len(s["text"].strip())
        if "Bold" in s["font"] or (s.get("flags", 0) & 16):
            bold += n
        else:
            regular += n
    return bold > regular


def _text_layer_lines(page: pymupdf.Page, pno: int, start_id: int) -> list[Line]:
    lines: list[Line] = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue  # images are handled separately as Figure items (_figures_for_page)
        for raw in block["lines"]:
            spans = [s for s in raw["spans"] if s["text"]]
            text = "".join(s["text"] for s in spans)
            if not text.strip():
                continue
            x0, y0, x1, y1 = raw["bbox"]
            lines.append(
                Line(
                    id=start_id + len(lines),
                    page=pno,
                    x0=x0, y0=y0, x1=x1, y1=y1,
                    size=round(max(s["size"] for s in spans), 1),
                    bold=_is_bold(spans),
                    text=clean_display_text(text),
                )
            )
    return lines


def _ocr_lines(page: pymupdf.Page, pno: int, start_id: int) -> list[Line]:
    """Rasterise a page and OCR it into Lines in PDF coordinates."""
    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(dpi=OCR_DPI)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    scale = 72.0 / OCR_DPI

    groups: dict[tuple, list[int]] = {}
    for i, word in enumerate(data["text"]):
        if word.strip() and float(data["conf"][i]) >= 0:
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            groups.setdefault(key, []).append(i)

    lines: list[Line] = []
    for idx in groups.values():
        x0 = min(data["left"][i] for i in idx) * scale
        y0 = min(data["top"][i] for i in idx) * scale
        x1 = max(data["left"][i] + data["width"][i] for i in idx) * scale
        y1 = max(data["top"][i] + data["height"][i] for i in idx) * scale
        text = " ".join(data["text"][i] for i in idx)
        lines.append(
            Line(
                id=start_id + len(lines),
                page=pno,
                x0=x0, y0=y0, x1=x1, y1=y1,
                size=round((y1 - y0) / 0.72, 1),  # cap-height-ish box -> rough pt size
                bold=None,
                text=clean_display_text(text),
                source="ocr",
            )
        )
    lines.sort(key=lambda l: (l.y0, l.x0))
    for k, line in enumerate(lines):
        line.id = start_id + k
    return lines


def _center_in(line: Line, rect: pymupdf.Rect, tol: float = 1.0) -> bool:
    cx, cy = (line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2
    return rect.x0 - tol <= cx <= rect.x1 + tol and rect.y0 - tol <= cy <= rect.y1 + tol


def _real_tables(found: list) -> list:
    """Filter PyMuPDF table candidates down to real tables.

    PyMuPDF 1.26-1.28 also returns small 1-row "tables" nested inside a real table (one per
    cell row or cell fragment); 2.0 does not. Without this filter those fragments claim the
    same lines a second time and content is duplicated (caught by the word-coverage test on
    a machine with 1.28 installed). Rules: a table needs at least 2 rows and 2 columns, and a
    candidate lying inside an already-accepted table is a fragment of it.
    """
    candidates = [t for t in found if t.row_count >= 2 and t.col_count >= 2]
    candidates.sort(key=lambda t: -pymupdf.Rect(t.bbox).get_area())  # biggest first
    kept: list = []
    for t in candidates:
        r = pymupdf.Rect(t.bbox)
        if any(pymupdf.Rect(k.bbox).contains(r) or _overlap_ratio(r, pymupdf.Rect(k.bbox)) > 0.5 for k in kept):
            continue
        kept.append(t)
    kept.sort(key=lambda t: t.bbox[1])
    return kept


def _overlap_ratio(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    inter = pymupdf.Rect(a) & b
    return 0.0 if inter.is_empty else inter.get_area() / max(a.get_area(), 1e-6)


def _tables_for_page(page: pymupdf.Page, pno: int, lines: list[Line]) -> list[Table]:
    tables: list[Table] = []
    claimed: set[int] = set()   # a line may belong to at most one table
    for t in _real_tables(page.find_tables().tables):
        grid: list[list[str]] = []
        used: list[int] = []
        for row in t.rows:
            cells_text = []
            for cell in row.cells:
                if cell is None:  # merged cell
                    cells_text.append("")
                    continue
                rect = pymupdf.Rect(cell)
                inside = sorted(
                    (l for l in lines if l.id not in claimed and _center_in(l, rect)),
                    key=lambda l: (l.y0, l.x0),
                )
                used.extend(l.id for l in inside)
                cells_text.append(" ".join(l.text for l in inside))
            grid.append(cells_text)
        # Any line inside the table box is owned by the table, even if it fell between cells;
        # stray ones are reported by the coverage check instead of leaking into paragraphs.
        box = pymupdf.Rect(t.bbox)
        owned = sorted({l.id for l in lines if l.id not in claimed and _center_in(l, box)})
        claimed.update(owned)
        tables.append(
            Table(
                page=pno,
                y0=t.bbox[1],
                y1=t.bbox[3],
                header=grid[0] if grid else [],
                rows=grid[1:],
                line_ids=owned,
                stray_line_ids=[lid for lid in owned if lid not in set(used)],
            )
        )
    return tables


def extract(pdf_path: str, force_ocr: bool = False) -> tuple[list[Item], list[Line], list[int], int]:
    """Return (ordered items, all lines, pages that used OCR, page count).

    Items are Lines not inside a table, one Table item per table and one Figure item per placed
    image, in reading order
    across ALL pages (one continuous stream, so page breaks do not split sections).
    """
    doc = pymupdf.open(pdf_path)
    items: list[Item] = []
    all_lines: list[Line] = []
    ocr_pages: list[int] = []
    for pno, page in enumerate(doc, start=1):
        start = len(all_lines)
        use_ocr = force_ocr or len(page.get_text().strip()) < MIN_TEXT_CHARS
        if use_ocr:
            lines = _ocr_lines(page, pno, start)
            ocr_pages.append(pno)
            tables: list[Table] = []  # geometry-based table detection needs vector lines/text layer
            figures: list[Figure] = []
        else:
            lines = _text_layer_lines(page, pno, start)
            tables = _tables_for_page(page, pno, lines)
            figures = _figures_for_page(page, pno)
        all_lines.extend(lines)

        in_table = {lid for t in tables for lid in t.line_ids}
        page_items: list[tuple[float, Item]] = [(l.y0, l) for l in lines if l.id not in in_table]
        page_items += [(t.y0, t) for t in tables]
        page_items += [(f.y0, f) for f in figures]
        page_items.sort(key=lambda p: p[0])
        items.extend(it for _, it in page_items)

    _merge_split_tables(items)
    return items, all_lines, ocr_pages, len(doc)


def _merge_split_tables(items: list[Item]) -> None:
    """A table that ends one page and continues at the top of the next, with nothing in
    between and the same column count, is merged into one (repeated header row dropped)."""
    i = 1
    while i < len(items):
        prev, cur = items[i - 1], items[i]
        if (
            isinstance(prev, Table)
            and isinstance(cur, Table)
            and cur.page == prev.page + 1
            and len(cur.header) == len(prev.header)
        ):
            rows = cur.rows if cur.header == prev.header else [cur.header] + cur.rows
            prev.rows.extend(rows)
            prev.line_ids.extend(cur.line_ids)
            prev.continued = True
            del items[i]
            continue
        i += 1
