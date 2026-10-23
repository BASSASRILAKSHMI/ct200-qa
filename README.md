# CT-200 QA Traceability Backend

Turns the CardioTrack CT-200 manual (PDF) into a structured, versioned document tree,
generates QA test-case ideas from selected sections with an LLM, and detects when those
test cases go stale after the manual changes.

> Built in stages. Done: Stage 1 (PDF parsing), Stage 2 (database and versioning),
> Stage 3 (browse and selection API), Stage 4 (LLM test-case generation). Next: staleness.

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

## Stage 3: the API

```bash
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/docs** for interactive Swagger UI (every endpoint can be tried
from the browser). Then, in a second terminal, the scripted end-to-end flow:

```bash
python scripts/demo_flow.py
```

It uploads v1, pins a selection of 3.2 + 4.2, uploads v2, and shows that the selection still
resolves to v1 text, that both sections changed, and exactly what changed (`40 -> 30` mmHg,
`2 -> 1.5` seconds, `E6` added). Each run uses its own document name, so it is repeatable.

| Method | Path | Purpose |
|---|---|---|
| POST | `/documents/ingest` | upload a PDF as the next version (201 new, 200 identical, 422 incomplete parse) |
| GET | `/documents/versions`, `/documents/versions/{n}` | versions with diff summary and parse warnings |
| GET | `/sections?version=` | top-level sections (latest by default) |
| GET | `/tree?version=` | whole heading tree |
| GET | `/nodes/{id}` | node with full text, blocks, hash, flags, parent, path, children |
| GET | `/nodes/{id}/changes` | changed since this version? history + word diff to latest |
| GET | `/search?q=&field=&flag=&version=` | search titles/text, filter by flag (e.g. `duplicate_title`) |
| POST | `/selections` | named, version-pinned selection (`include_descendants` for chapters) |
| GET | `/selections`, `/selections/{id}` | selections with pinned text and status in latest version |

All browse endpoints also take `document=` (default `CT-200 Manual`).

IDs: a **node id** identifies one section *in one version* and never changes meaning; the
**logical_node_id** is shared by that section across versions.

curl examples (on Windows PowerShell type `curl.exe`, not `curl`):

```bash
curl -F "file=@data/ct200_manual.pdf" http://127.0.0.1:8000/documents/ingest
curl -F "file=@data/ct200_manual_v2.pdf" http://127.0.0.1:8000/documents/ingest
curl "http://127.0.0.1:8000/sections?version=1"
curl "http://127.0.0.1:8000/search?q=error%20codes&field=title"
curl "http://127.0.0.1:8000/search?flag=out_of_order"
curl http://127.0.0.1:8000/nodes/11/changes
curl -H "Content-Type: application/json" -d "{\"name\":\"inflation\",\"node_ids\":[11,16]}" http://127.0.0.1:8000/selections
```

Selection rules: all nodes from one version (422 otherwise), unknown ids 404, name reused 409,
order normalised to document order, duplicates collapsed.

## Stage 4: LLM test-case generation

### Configure (once)

```bash
cp .env.example .env          # Windows: copy .env.example .env
```

Edit `.env`:
* `GROQ_API_KEY=` your key from https://console.groq.com (API Keys)
* `MONGODB_URI=` your Atlas connection string (Connect → Drivers), or leave empty to store
  results as JSON files in `./generation_store`

`.env` is git-ignored. Then check it:

```bash
python scripts/check_setup.py
```

### Use

```bash
uvicorn app.main:app --reload
python scripts/demo_flow.py          # now also generates, and shows the duplicate policy
```

| Method | Path | Purpose |
|---|---|---|
| POST | `/selections/{id}/generate?force=` | 201 new · 200 identical earlier result reused (no LLM call) · 502 failed (stored, inspectable) · 422 headings only |
| GET | `/generations/{id}?include_raw=` | one generation with sources, test cases and every attempt |
| GET | `/selections/{id}/generations` | all generations for a selection, newest first |

Every test case cites `source_node_ids` and a `requirement_quote` that the validator checks
word for word against the cited section. Prompt design, validation layers, retry and duplicate
policies: [docs/llm_generation.md](docs/llm_generation.md).

The test suite never calls a real LLM or MongoDB: it uses a scripted LLM, an in-memory store and
`mongomock`, so `pytest` needs no keys or network.

## Layout

```
app/parsing/     normalize, extract, classify, tree, parser
app/db/          SQLAlchemy engine and models
app/versioning/  cross-version matcher, word diff
app/services/    ingestion, queries, selections (no HTTP code)
app/llm/         LLM client (OpenAI-compatible, Groq default) + scripted test double
app/generation/  prompt, reply validation, generation service
app/storage/     generation store: MongoDB, JSON files, in-memory
app/api/         FastAPI routes, schemas, converters; app/main.py wires them up
scripts/         inspect_pdf, parse_cli, ocr_crosscheck, ingest, show_history, demo_flow, check_setup
tests/           real-PDF tests + synthetic edge-case tests
data/            ct200_manual.pdf (v1), ct200_manual_v2.pdf (v2)
docs/            findings, versioning, approach document
```
