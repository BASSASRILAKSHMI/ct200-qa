# LLM test-case generation

`POST /selections/{id}/generate` turns the pinned text of a selection into 3–5 QA test-case
ideas, validates them, and stores the result (with its exact sources) in the NoSQL store.

```
selection (pinned node versions)
   -> prompt (sections as tagged data + JSON spec)
   -> LLM (Groq, OpenAI-compatible, JSON mode)        transport retries live here
   -> validate (parse -> schema -> traceability -> grounding)
        rejected? send the exact errors back, try again (max 3 replies)
   -> store: succeeded, or failed with every raw attempt
```

## Prompt design (`app/generation/prompt.py`)

* **Sections are data, not instructions.** Each is wrapped in
  `<section node_id="16" heading="4.2 Error Codes">…</section>`, and the system prompt says
  text inside those tags is source material. This limits prompt injection from the manual itself.
* **The output contract is shown, not described**: an example object with every field, the
  allowed enum values, and the 3–5 count. JSON mode is requested from the provider, and the word
  "JSON" appears in the prompt (Groq requires it for JSON mode).
* **Traceability is built into the output.** Each test case must list `source_node_ids` (only ids
  that were sent) and a `requirement_quote` copied word for word from a cited section.
* **Domain steer**: prefer exact thresholds, timings, error codes, boundaries just inside/outside a
  limit, and safety behaviour, because that is where a home BP monitor's real risks are.
* `PROMPT_VERSION = "tc-v1"` is stored on every result and is part of the duplicate key.
* Temperature 0.2: the task rewards consistency, not creativity.
* Model: `openai/gpt-oss-120b` on Groq by default, set by `LLM_MODEL`. The first choice,
  `llama-3.3-70b-versatile`, was retired by Groq on 2026-08-16 and returned `model_not_found`
  during setup. That is why the model is configuration, part of the duplicate key, and
  `scripts/check_setup.py` lists the models a key can use when the configured one is refused.

## Validation (`app/generation/validate.py`)

Four layers. A reply must pass all of them **as a whole batch**.

| Layer | Catches | Example error sent back to the model |
|---|---|---|
| 1. Completion | cut off by length limit, empty reply, provider JSON-mode rejection | `the reply was cut off before the JSON ended` |
| 2. Parse | prose, truncated JSON, a list instead of an object | `the reply is not valid JSON (Expecting value at line 1, column 1)` |
| 3. Schema (Pydantic) | missing/blank fields, bad enum values, empty steps, title length | `test_cases.0.type: Input should be 'functional', …` |
| 4. Meaning | 3–5 cases; cited node ids were actually sent; quote really appears in the cited section; distinct titles | `test case 3 requirement_quote is not copied word for word from its cited section(s) [16]` |

Layer 4 is what makes this more than "valid JSON". In testing, a reply claiming E3 deflates
"within 0.5 seconds" was perfectly valid JSON and would pass any schema check. The quote check
rejects it because the manual says 2 seconds.

**Leniency policy.** Only mechanical wrapping is repaired: a ```` ```json ```` fence around an
otherwise valid object is stripped, and the repair is recorded on the attempt. Content is never
repaired: no dropping the one bad case to keep four good ones, no trimming 6 cases to 5, no
re-mapping an unknown node id. Extra fields the model adds (`id`, `confidence`) are ignored,
because they cannot make a result wrong and rejecting them would only burn retries.

**Warnings, not errors.** If an explicitly selected section with text is not cited by any test
case, the result is accepted with a warning. 3–5 cases cannot always cover 6 sections, and
rejecting would loop.

## Failure handling

| Situation | Where handled | What happens | HTTP |
|---|---|---|---|
| 429 / 5xx / timeout / unreadable 200 | LLM client | exponential backoff (1, 2, 4 s; honours `Retry-After`), up to 3 retries | — |
| retries exhausted | service | stored `failed`, `failure_reason = llm_unavailable` | 502 |
| 400/401/403/404 (bad key, unknown model) | LLM client | no retry (retrying cannot fix it); stored `failed`, `llm_config_error` | 502 |
| reply rejected by validator | service | errors sent back as the next user turn; up to 3 replies | — |
| 3 rejected replies | service | stored `failed`, `invalid_output`, **all raw replies kept** | 502 |
| selection has only empty headings | service | refused before any LLM call | 422 |

A failed generation is still a stored, retrievable record (`GET /generations/{id}?include_raw=true`),
so "why did this fail?" can always be answered later. It never contains test cases.

## Duplicate-submission policy

`request_key = sha256(selection fingerprint | prompt version | model)`. The selection fingerprint
already covers the exact node versions **and** their content hashes.

* **Same key already succeeded** → the stored result is returned with `reused: true` (HTTP 200)
  and the LLM is **not** called. LLM output is non-deterministic: calling again would give a
  different answer to an identical question. For traceability, "the test cases for this exact
  text" should have one answer, and repeat calls cost money and quota.
* **`force=true`** → a new generation is made; it records `supersedes: <previous id>`. Nothing is
  overwritten, so earlier results stay auditable.
* **Failed results are never reused**, so resubmitting after a failure really retries.
* **Two selections with the same nodes** share the result, because the key is about the text,
  not the selection's name.
* **A different prompt version or model** is a different request.

Limit: identical concurrent requests are serialised with an in-process lock per key. Several
server processes could still race and make two LLM calls; a unique index on `request_key` for
successful records in MongoDB would close that.

## Storage (`app/storage/generations.py`)

Generations go to MongoDB (Atlas) when `MONGODB_URI` is set, otherwise to one JSON file per
generation in `./generation_store` (atomic writes). Both answer the same queries; tests run the
service against an in-memory store and against `mongomock`.

Why a document store for this part: a generation is write-once, read whole, never joined, and
its shape is nested and variable (cases, steps, attempts, errors). The relational side keeps
what needs integrity: versions, parent links, selections.

Each record holds a **snapshot of its sources**:

```json
"sources": [{"node_id": 11, "logical_node_id": 11, "version": 1, "number": "3.2",
             "title": "Cuff Inflation Sequence", "content_hash": "a1f7315252…"}]
```

This is what survives re-versioning: staleness (next stage) compares each source's
`content_hash` with the latest version of the same `logical_node_id`. Old node rows never
change, so the record always points at exactly the text it was generated from.

## Known limitations

* The quote check proves a test is **anchored** to real text, not that its steps and expected
  result are correct. A model can quote "E3 … within 2 seconds" and still write a wrong expected
  result. A second check (numbers in `expected_result` must appear in the cited text) is the
  obvious next step.
* No chunking: a very large selection could exceed the model's context. The CT-200 manual is far
  below any limit.
* The JSON-file store scans every file on `find()`. Fine for hundreds of records, not thousands.
* Prompt-injection mitigation is a prompt instruction plus tagging, not a guarantee.
