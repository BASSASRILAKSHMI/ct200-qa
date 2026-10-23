# CT-200 QA Traceability Backend

Turns the CardioTrack CT-200 manual (PDF) into a structured, versioned document tree,
generates QA test-case ideas from selected sections with an LLM, and detects when those
test cases go stale after the manual changes.

> Work in progress — built in stages. Full setup and API docs are added as each stage lands.

## Layout

```
app/parsing/   PDF -> lines -> blocks -> section tree
scripts/       inspection and CLI helpers
tests/         unit tests (parser irregularities first)
data/          ct200_manual.pdf (v1), ct200_manual_v2.pdf (v2)
docs/          findings and approach document
```
