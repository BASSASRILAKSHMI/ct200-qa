# CT-200 QA Traceability Backend

A FastAPI backend that turns the CardioTrack CT-200 manual (PDF) into a **versioned section
tree**, generates **QA test-case ideas** from selected sections with an LLM, and tells you, when
you fetch those test cases later, **whether they still match the current manual** and how
serious any change is.

* Parses the PDF's real structure, including its traps (a skipped level, out-of-order sections,
  a numbered list that looks like chapters, duplicate titles, page-split paragraphs, tables).
* Re-ingesting v2 keeps v1 intact; unchanged sections are recognised as the same logical section,
  changed ones are flagged with a word diff.
* Selections are pinned to exact section versions.
* LLM output is validated strictly (schema, cited sections, verbatim quotes) with feedback
  retries; failures are stored, never half-accepted; duplicate submissions reuse results.
* Staleness is per test case: **stale / review / current**, with severity **high** when a value it
  depends on changed (e.g. `40 → 30 mmHg`, `2 → 1.5 s`).

**Design and decisions:** [docs/APPROACH.md](docs/APPROACH.md) (includes the decision log; PDF copy: [docs/APPROACH.pdf](docs/APPROACH.pdf)).

## Setup

Requires **Python 3.10+**. Tesseract is optional (only for the OCR fallback and cross-check).

```bash
git clone https://github.com/BASSASRILAKSHMI/ct200-qa.git
cd ct200-qa
python -m venv .venv
source .venv/bin/activate              # Windows PowerShell: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                   # Windows: copy .env.example .env
```

### Environment variables (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | (none) | LLM key from https://console.groq.com (needed only for generation) |
| `LLM_MODEL` | `openai/gpt-oss-120b` | any model your key can use |
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | any OpenAI-compatible endpoint |
| `LLM_TEMPERATURE` | `0.2` | |
| `LLM_MAX_CONTENT_ATTEMPTS` | `3` | replies the validator may reject before a generation is stored as failed |
| `LLM_MAX_TRANSPORT_RETRIES` | `3` | retries for 429 / 5xx / timeouts |
| `MONGODB_URI` | (empty) | MongoDB/Atlas connection string; **empty = JSON files** in `./generation_store` |
| `MONGODB_DB` | `ct200_qa` | |
| `DATABASE_URL` | `sqlite:///./ct200.db` | tree, versions, selections |

`.env` is git-ignored. Check it (prints no secrets, lists usable models if one is refused):

```bash
python scripts/check_setup.py
```

## Run

```bash
uvicorn app.main:app --reload
```

Interactive API docs: **http://127.0.0.1:8000/docs**

## The v1 → v2 re-ingestion flow

**Scripted, end to end** (server running, second terminal):

```bash
python scripts/demo_flow.py
```

It uploads v1, pins a selection of 3.2 + 4.2, generates test cases, submits again (reused, no LLM
call), uploads v2, then shows: the selection still resolves to v1 text, exactly what changed
(`40 → 30` mmHg, `2 → 1.5` s, `E6` added), which generated test cases are now stale and why, and
that they can be found from the v2 node. Each run uses its own document name, so it is repeatable.
Without `GROQ_API_KEY` the generation step reports the configuration error and the rest still runs.

**By hand with curl** (PowerShell: type `curl.exe`):

```bash
# 1. ingest v1, then v2 as version 2 of the same document
curl -F "file=@data/ct200_manual.pdf"    http://127.0.0.1:8000/documents/ingest
curl -F "file=@data/ct200_manual_v2.pdf" http://127.0.0.1:8000/documents/ingest
#    -> 201 with diff_summary: unchanged 24, modified [2.1.1.1, 3.2, 4.2, 4.3], added [5.3]
#    re-uploading the same file -> 200, created=false

# 2. find v1's section 3.2 and see what changed
curl "http://127.0.0.1:8000/search?q=3.2&field=title&version=1"     # note its id, e.g. 11
curl  http://127.0.0.1:8000/nodes/11/changes

# 3. pin a selection, generate, check staleness
curl -H "Content-Type: application/json" -d "{\"name\":\"inflation\",\"node_ids\":[11]}" http://127.0.0.1:8000/selections
curl -X POST http://127.0.0.1:8000/selections/1/generate
curl "http://127.0.0.1:8000/selections/1/generations"               # staleness block per generation
```

Node 11 is v1's 3.2, so the selection is pinned to v1 text and its test cases are judged against
v2 immediately: the generate response and every retrieval already carry the staleness verdict.
Without the API, the same versioning flow runs from the command line:

```bash
python scripts/ingest.py data/ct200_manual.pdf data/ct200_manual_v2.pdf --reset
python scripts/show_history.py 3.2          # every version of 3.2 with a word diff
```

## Tests

```bash
python -m pytest -q          # 128 tests, ~15 s, no network or API key needed
```

The LLM is replaced by a scripted test double, the generation store by an in-memory store and
`mongomock`, so tests are deterministic and never touch your `.env` services. Verified on
Python 3.10 and 3.13, PyMuPDF 1.26–2.0.

| Area | File |
|---|---|
| every PDF irregularity, word coverage, hash stability | `tests/test_parser_ct200.py` |
| parser edge cases not in the PDF, figures and captions | `tests/test_parser_synthetic.py`, `tests/test_parser_figures.py` |
| version matcher, including refusal cases | `tests/test_matcher.py` |
| ingestion, idempotency, revert, concurrent uploads | `tests/test_ingestion.py` |
| browse and selection API | `tests/test_api.py` |
| LLM transport errors | `tests/test_llm_client.py` |
| malformed / ungrounded LLM output, duplicate policy | `tests/test_generation.py`, `tests/test_generation_api.py` |
| staleness rules and real v1 → v2 → v3 history | `tests/test_staleness.py`, `tests/test_staleness_api.py` |

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/documents/ingest?document=` | upload a PDF as the next version (201 new · 200 identical · 409 concurrent · 422 incomplete parse) |
| GET | `/documents/versions`, `/documents/versions/{n}` | versions with diff summary and parse warnings |
| GET | `/sections?version=` | top-level sections (latest by default) |
| GET | `/tree?version=` | whole heading tree |
| GET | `/nodes/{id}` | full text, blocks, content hash, flags, parent, path, children |
| GET | `/nodes/{id}/changes` | changed since this version? history + word diff to latest |
| GET | `/search?q=&field=&flag=&version=` | search titles/text; filter by flag (`duplicate_title`, `out_of_order`, …) |
| POST | `/selections` | named, version-pinned selection (`include_descendants` for chapters) |
| GET | `/selections`, `/selections/{id}` | with pinned text and status in the latest version |
| POST | `/selections/{id}/generate?force=` | 201 new · 200 reused (no LLM call) · 502 failed (stored) · 422 headings only |
| GET | `/generations/{id}?include_raw=` | test cases, sources, attempts, **staleness** |
| GET | `/selections/{id}/generations?status=` | by selection; filter `current` / `review` / `stale` |
| GET | `/nodes/{id}/generations?exact_version=&status=` | by section across versions |

A **node id** identifies a section *in one version* and never changes meaning; the
**logical_node_id** is shared by that section across versions.

## Project layout

```
app/
  parsing/     PDF -> lines -> headings/paragraphs/lists/tables -> tree (+ coverage check)
  db/          SQLAlchemy models: documents, versions, logical nodes, node versions, selections
  versioning/  cross-version matcher, word diff with changed numbers
  services/    ingestion, queries, selections (no HTTP code)
  llm/         OpenAI-compatible client (Groq default) + scripted test double
  generation/  prompt, reply validation, generation service
  storage/     generation store: MongoDB, JSON files, in-memory
  staleness/   per-test-case staleness against the latest version
  api/         FastAPI routes, schemas, converters;  app/main.py wires them up
scripts/       inspect_pdf, parse_cli, ocr_crosscheck, ingest, show_history, check_setup, demo_flow
tests/         128 tests
data/          ct200_manual.pdf (v1), ct200_manual_v2.pdf (v2)
docs/          APPROACH.md, pdf_findings.md, versioning.md, llm_generation.md, staleness.md
```
