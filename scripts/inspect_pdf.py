"""Dump the raw layout of a PDF line by line: page, y, x, font size, bold flag, text.

This was the first thing written for the project. Every parsing rule in
app/parsing/ comes from something visible in this dump (see docs/pdf_findings.md).

Usage:
    python scripts/inspect_pdf.py data/ct200_manual.pdf
    python scripts/inspect_pdf.py data/ct200_manual.pdf --bold-only
"""
import argparse
import sys

import pymupdf


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--bold-only", action="store_true", help="show only bold lines (heading candidates)")
    args = ap.parse_args()

    doc = pymupdf.open(args.pdf)
    print(f"{args.pdf}: {len(doc)} pages")
    for pno, page in enumerate(doc, start=1):
        tables = page.find_tables().tables
        has_text = bool(page.get_text().strip())
        print(f"\n===== page {pno}  text_layer={has_text}  tables={[tuple(round(v) for v in t.bbox) for t in tables]}")
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                print(f"  [image block] bbox={tuple(round(v) for v in block['bbox'])}")
                continue
            for line in block["lines"]:
                spans = line["spans"]
                text = "".join(s["text"] for s in spans)
                first = spans[0]
                bold = "Bold" in first["font"]
                if args.bold_only and not bold:
                    continue
                print(
                    f"  y={first['bbox'][1]:6.1f} x={first['bbox'][0]:5.1f} "
                    f"size={first['size']:4.1f} {'B' if bold else ' '} | {text}"
                )


if __name__ == "__main__":
    sys.exit(main())
