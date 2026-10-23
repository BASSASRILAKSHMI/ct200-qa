"""Public entry point: parse_pdf(path) -> ParseResult.

    PDF --extract--> Lines + Tables --classify--> elements --build_tree--> Nodes
                                                 \\--> coverage check (no line may vanish)
"""
from __future__ import annotations

from collections import Counter

from .classify import classify
from .extract import extract
from .models import ParseResult, Table
from .tree import build_tree


def parse_pdf(pdf_path: str, force_ocr: bool = False) -> ParseResult:
    items, lines, ocr_pages, page_count = extract(pdf_path, force_ocr=force_ocr)
    elements, consumed, _layout = classify(items, lines)
    nodes, warnings = build_tree(elements)

    # Coverage check: every extracted line must belong to EXACTLY one element.
    unassigned = [l for l in lines if l.id not in consumed]
    for l in unassigned:
        warnings.append(f"unassigned line on page {l.page}: {l.text!r}")
    claims = Counter(lid for el in elements for lid in el.line_ids)
    duplicated = sorted(lid for lid, n in claims.items() if n > 1)
    for lid in duplicated:
        warnings.append(f"line claimed by {claims[lid]} elements (content duplicated): {lines[lid].text!r}")
    for t in (it for it in items if isinstance(it, Table)):
        for lid in t.stray_line_ids:
            warnings.append(f"line inside table on page {t.page} not in any cell: {lines[lid].text!r}")
        if t.continued:
            warnings.append(f"table starting on page {t.page} continued onto the next page (merged)")
    for el in elements:
        blk = getattr(el, "block", None)
        if blk is not None and blk.kind == "figure":
            cap = f"caption {blk.text!r}" if blk.text else "no caption found"
            warnings.append(f"figure on page {blk.page} kept as a figure block ({cap}); image content not extracted")
    if ocr_pages:
        warnings.append(f"OCR used for pages {ocr_pages}: no font weight, heading detection is text-only")

    return ParseResult(
        source=pdf_path,
        page_count=page_count,
        nodes=nodes,
        warnings=warnings,
        unassigned_lines=unassigned,
        duplicated_line_ids=duplicated,
        ocr_pages=ocr_pages,
    )
