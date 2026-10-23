"""Validate the text-layer parse against an independent OCR parse of the same PDF.

The two pipelines share no text source: one reads embedded characters and font weights, the
other only sees rendered pixels. If both produce the same section numbers, titles and
parents, the hierarchy is very unlikely to be an artefact of either method.

Body text is compared word by word; differences are listed so a human can decide which side
is wrong (on the CT-200 it is always OCR: '±3' -> '+3', 'Class II' -> 'Class Il').

Usage:
    python scripts/ocr_crosscheck.py data/ct200_manual.pdf
Exit code 1 if the hierarchies disagree.
"""
import difflib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.parsing.parser import parse_pdf  # noqa: E402


def main(pdf: str) -> int:
    text = parse_pdf(pdf)
    ocr = parse_pdf(pdf, force_ocr=True)

    def shape(r):
        return [(n.key, n.title, n.parent_key, n.level) for n in r.nodes.values() if n.key != "doc"]

    s_text, s_ocr = shape(text), shape(ocr)
    structure_ok = s_text == s_ocr
    print(f"Hierarchy: text-layer {len(s_text)} sections, OCR {len(s_ocr)} sections -> "
          f"{'IDENTICAL' if structure_ok else 'DIFFERENT'}")
    if not structure_ok:
        for line in difflib.unified_diff([str(x) for x in s_text], [str(x) for x in s_ocr], "text", "ocr", lineterm=""):
            print("  ", line)

    print("\nBody text agreement (word level):")
    for key, node in text.nodes.items():
        other = ocr.nodes.get(key)
        if other is None or not node.body:
            continue
        a, b = node.body.split(), other.body.split()
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        if ratio < 1.0:
            diffs = [
                (" ".join(a[i1:i2]) or "∅", " ".join(b[j1:j2]) or "∅")
                for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes()
                if op != "equal"
            ]
            kinds = ""
            if [x.kind for x in node.blocks] != [x.kind for x in other.blocks]:
                kinds = (f"  (block types differ: {[x.kind for x in node.blocks]} vs "
                         f"{[x.kind for x in other.blocks]})")
            print(f"  {key:8} {ratio:5.1%}  e.g. {diffs[:3]}{kinds}")
    return 0 if structure_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "data/ct200_manual.pdf"))
