# CT-200 QA Traceability Backend

Turns the CardioTrack CT-200 manual (PDF) into a structured, versioned document tree,
generates QA test-case ideas from selected sections with an LLM, and detects when those
test cases go stale after the manual changes.

> Built in stages. Stage 1 (PDF parsing) is complete; API, versioning, LLM generation and
> staleness are added in later stages.

## Setup

Requires Python 3.11+ and, for the OCR fallback/cross-check only, the Tesseract binary
(`sudo apt install tesseract-ocr`, or the Windows installer from UB Mannheim).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Stage 1: PDF parsing

```bash
# raw layout dump (fonts, sizes, positions) - how every parsing rule was discovered
python scripts/inspect_pdf.py data/ct200_manual.pdf

# parsed tree with flags and content hashes
python scripts/parse_cli.py data/ct200_manual.pdf
python scripts/parse_cli.py data/ct200_manual_v2.pdf --json out/v2.json

# independent OCR parse compared against the text-layer parse (~30 s)
python scripts/ocr_crosscheck.py data/ct200_manual.pdf

# tests
python -m pytest -q
```

Pipeline: `extract` (text layer, OCR fallback, table geometry) -> `classify` (title, headings,
paragraphs, lists, tables) -> `build_tree` (numbering-based parents, irregularity flags) ->
coverage check. Details and every irregularity found: [docs/pdf_findings.md](docs/pdf_findings.md).

## Layout

```
app/parsing/   normalize, extract, classify, tree, parser
scripts/       inspect_pdf, parse_cli, ocr_crosscheck
tests/         real-PDF tests + synthetic edge-case tests
data/          ct200_manual.pdf (v1), ct200_manual_v2.pdf (v2)
docs/          findings and approach document
```
