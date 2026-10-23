# Document versioning and cross-version matching

## Data model

```
Document 1--* DocumentVersion 1--* NodeVersion *--1 LogicalNode
```

* **LogicalNode**: the identity of a section across versions ("4.2 Error Codes" as a concept).
* **NodeVersion**: that section's exact text, structure, flags and content hash in **one**
  version. Rows are only ever inserted, never updated, so a version-1 row keeps its v1 text
  forever. `parent_id` points to the parent *in the same version*; `previous_id` points to the
  same logical section in the previous version.
* Selections and generated test cases (later stages) reference **NodeVersion ids**. That is what
  makes them version-pinned: re-ingesting the manual adds rows and cannot change the text an
  old selection points to.

Ingestion policy:
* The same file bytes as the latest version → no new version (idempotent).
* The same bytes as an older version → a new version (a revert is a real event).
* The parser's coverage check failed (a line lost or claimed twice) → ingestion refused,
  nothing written. A tree with missing content must never become a traceability baseline.
* One transaction per ingestion.

## Matching strategy

Matching runs between the new parse and the latest stored version, in passes from strictest
to loosest. A pass claims a pair only when the candidate is **unique on both sides**;
otherwise the section falls through to the next pass.

| Pass | Evidence | Handles |
|---|---|---|
| exact | same number + same normalised title | the normal case (all 28 CT-200 sections) |
| title_parent | same title, parent already matched to this node's parent; repeated until stable | renumbering, e.g. 3.4/3.3 put back in order |
| number_title~ | same number, title similarity ≥ 0.6 | reworded heading |
| same_body | identical hash of a non-empty body (≥ 40 chars) | moved + renamed, text untouched |
| body~ | word-level body similarity ≥ 0.85, winner ≥ 0.05 ahead of the runner-up | moved + renamed + lightly edited |
| (none) | — | new side → `added`, old side → `removed` |

Status of a matched node: `unchanged` if the content hash is equal, otherwise `modified`;
plus `title_changed` and `moved` (number or parent changed) as separate booleans.

### Why this design

* **Not hash-first.** Every chapter heading with no body of its own hashes to the same value
  (the hash of ""), and identical boilerplate paragraphs would collide too. Hashes say whether
  text changed, not which section is which.
* **Not path-only.** Matching only by section number turns any renumbering into
  "removed + added" and orphans every test case linked to the section.
* **Not fuzzy-first.** A fuzzy title match on its own would happily pair 4.2 and 7.1
  ("Error Codes" twice). Scoping the title match to the matched parent is what keeps duplicates
  apart.
* **Refuse rather than guess.** A missed match is visible: the section shows as removed +
  added, and its test cases show as orphaned. A wrong match is invisible: old test cases get
  silently attached to different text. In a medical-device QA context the second failure is the
  dangerous one, so every ambiguous case falls through.

### Result on the real manual (v1 → v2)

24 unchanged, 4 modified (2.1.1.1, 3.2, 4.2, 4.3), 1 added (5.3), 0 removed, no ambiguity notes.
Every match came from the `exact` pass. The other passes are exercised by
`tests/test_matcher.py` on synthetic versions.

## Where it breaks (known failure modes)

1. **Section split in two** (4.2 becomes 4.2 + 4.3). At most one half keeps the old identity
   (by title or body similarity); the other is `added`. The test cases on the old section are
   only checked against one half.
2. **Two sections merged.** One predecessor matches; the other shows as `removed` even though
   its text survives inside the merged section.
3. **Same-title siblings renumbered and reworded together** (two "Warnings" under one parent).
   The title pass refuses; if a rewording drops body similarity below 0.85, lineage is lost and
   the section shows as removed + added (tested in `test_ambiguous_same_title_siblings...`).
4. **Heavy rewrite with the same number and a new title** (similarity < 0.6). Treated as a
   different section: correct if the topic really changed, wrong if it was only rephrased.
5. **Only the previous version is compared.** A section deleted in v2 and restored in v3 gets a
   new logical id in v3, not its v1 id. Fixable by matching unmatched v3 nodes against
   `removed` lists of earlier versions.
6. **Thresholds are tuned on judgement, not data.** 0.6 / 0.85 / 0.05 are reasonable for prose
   sections of this length; very short bodies (< 40 chars) skip the body passes entirely.

## Diff summary

`app/versioning/diff.py` gives a word-level diff, plus the numbers that changed. Numbers are
taken from the changed spans, and those on both sides of one replacement cancel, so:

| Section | Reported change |
|---|---|
| 3.2 | `40` → `30` (the new "original 40 mmHg" sentence does not hide it) |
| 4.2 | `2` → `1.5`, `E6` added |
| 4.3 | `E5` → `E6` (not "E1 removed, E1 added") |
| 2.1.1.1 | `300` → `250`, `15%` → `10%` |

This numeric signal is the input to staleness severity in a later stage.
