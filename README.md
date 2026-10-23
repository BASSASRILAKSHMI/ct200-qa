# CT-200 QA Traceability Backend

Turns the CardioTrack CT-200 manual (PDF) into a structured, versioned document tree,
generates QA test-case ideas from selected sections with an LLM, and detects when those
test cases go stale after the manual changes.

> Built in stages. Done: Stage 1 (PDF parsing), Stage 2 (database and versioning).
> Next: browse/selection API, LLM generation, staleness.

## Setup

Requires Python 3.10+ and, for the OCR fallback/cross-check only, the Tesseract binary
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

## Stage 2: versions in SQLite (v1 -> v2 re-ingestion)

```bash
# ingest v1, then v2 as version 2 of the same document (--reset starts from an empty DB)
python scripts/ingest.py data/ct200_manual.pdf data/ct200_manual_v2.pdf --reset

# or one at a time, which is what a real update looks like
python scripts/ingest.py data/ct200_manual.pdf
python scripts/ingest.py data/ct200_manual_v2.pdf

# history of one section across versions, with a word diff and changed numbers
python scripts/show_history.py 3.2
python scripts/show_history.py "Error Codes"
```

Expected v1 -> v2 result: 24 unchanged, 4 modified (2.1.1.1, 3.2, 4.2, 4.3), 1 added (5.3).
Re-running the v2 ingest is a no-op. The database is `ct200.db` (set `DATABASE_URL` to change).
Matching strategy and its failure modes: [docs/versioning.md](docs/versioning.md).

## Layout

```
app/parsing/     normalize, extract, classify, tree, parser
app/db/          SQLAlchemy engine and models
app/versioning/  cross-version matcher, word diff
app/services/    ingestion
scripts/         inspect_pdf, parse_cli, ocr_crosscheck, ingest, show_history
tests/           real-PDF tests + synthetic edge-case tests
data/            ct200_manual.pdf (v1), ct200_manual_v2.pdf (v2)
docs/            findings, versioning, approach document
```
