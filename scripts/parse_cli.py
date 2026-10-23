"""Print the parsed tree of a PDF (or dump it as JSON) for manual review.

Usage:
    python scripts/parse_cli.py data/ct200_manual.pdf
    python scripts/parse_cli.py data/ct200_manual.pdf --json out/v1.json
    python scripts/parse_cli.py data/ct200_manual.pdf --ocr      # force the OCR path
"""
import argparse
import json
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.parsing.parser import parse_pdf  # noqa: E402


def print_tree(result, key="doc", depth=0, show_body=True):
    n = result.nodes[key]
    label = f"{n.number} {n.title}" if n.number else (n.title or "(root)")
    flags = f"  [{', '.join(n.flags)}]" if n.flags else ""
    print(f"{'    ' * depth}{label}  (key={n.key}, L{n.level}, p{n.page}, hash={n.content_hash[:10]}){flags}")
    if show_body:
        for b in n.blocks:
            preview = b.render().replace("\n", " / ")
            print(f"{'    ' * depth}  - {b.kind}: {preview[:110]}{'...' if len(preview) > 110 else ''}")
    for c in n.children:
        print_tree(result, c, depth + 1, show_body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--json", help="write nodes to this JSON file")
    ap.add_argument("--ocr", action="store_true", help="force OCR for every page")
    ap.add_argument("--no-body", action="store_true")
    args = ap.parse_args()

    r = parse_pdf(args.pdf, force_ocr=args.ocr)
    print_tree(r, show_body=not args.no_body)
    print(f"\n{len(r.nodes) - 1} sections, {r.page_count} pages, OCR pages: {r.ocr_pages or 'none'}")
    print("Warnings:" if r.warnings else "Warnings: none")
    for w in r.warnings:
        print("  -", w)

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        payload = {
            k: {**asdict(n), "body": n.body, "content_hash": n.content_hash} for k, n in r.nodes.items()
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"\nJSON written to {args.json}")


if __name__ == "__main__":
    main()
