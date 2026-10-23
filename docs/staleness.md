# Staleness: do old test cases still match the manual?

A generation is created from specific node versions. When the manual is re-ingested, its test
cases may no longer describe the device. Every endpoint that returns a generation
(`GET /generations/{id}`, `GET /selections/{id}/generations`, `GET /nodes/{id}/generations`,
and the generate call itself) includes a `staleness` block and, on each test case,
`stale_status` / `stale_severity`.

## Computed at read time, never stored

Staleness is recomputed on every request against the latest version that exists right now.
A stored "stale" flag would itself go stale the moment another version is ingested (or the
manual is reverted, which makes old tests valid again, as tested). The cost is a few hash
comparisons and short word diffs per generation, which is negligible at this scale.

What makes this possible is the snapshot stored with every generation:
`sources: [{node_id, logical_node_id, version, content_hash, ...}]`. The old NodeVersion rows
never change, and the logical id links them to the current version.

## Three levels

**1. Source level (certain).** For each source, the latest NodeVersion with the same logical id:
same `content_hash` → `unchanged`; different → `changed` (with a word diff); none → `removed`.

**2. Quote level (per test case).** Each test case carries a `requirement_quote` that was verified
verbatim at generation time. If a cited source changed, is that quote still in its current text?
* gone → **stale**: the requirement the test was written against changed;
* still there → **review**: the section changed, but not the sentence this test is anchored to.

**3. Severity.** A wrong threshold in a medical-device test matters more than a reworded sentence.

| Severity | Rule | Example (real v1 → v2) |
|---|---|---|
| high | a cited section was removed, or a number/code in the quote or expected result was changed by the edit | "40 mmHg increments" → 30; "within 2 seconds" → 1.5 s; "E1–E5" → "E1–E6" |
| medium | the quote changed, but no value the test depends on did; or the quote is intact but a value used in the expected result changed elsewhere | "the valve opens" → "the valve releases" |
| low | the quote is intact; other text in the section changed | 3.2's "initial target of 180 mmHg", unchanged while its increment changed |
| none | nothing cited changed | 7.1 between v1 and v2 |

A generation's overall status and severity are the worst of its test cases.

## Does a one-word change count the same as a changed pressure threshold?

No, and the assignment asks exactly this. Both are **stale** if the changed words are inside the
quoted requirement, because the test was written against text that no longer exists, and a
human should look. But the threshold change is **high** and the wording change is **medium**, so a
reviewer can triage. Both cases are unit-tested (`test_one_word_wording_change...`,
`test_changed_threshold...`).

Treating them as equal would either bury real safety regressions in noise, or ignore wording
changes that can matter ("must" → "should"). Severity keeps both visible and ordered.

## The real result

Groq produced four test cases for 3.2 + 4.2 from v1. After v2 was ingested:

| Test case | Verdict | Why |
|---|---|---|
| inflation reaches the 180 mmHg target | review / low | 3.2 changed, but "initial target of 180 mmHg" is still there |
| inflation in 40 mmHg increments | **stale / high** | quote gone; 40 → 30 |
| E3 auto-deflates within 2 seconds | **stale / high** | quote gone; 2 → 1.5 |
| E1 aborts the measurement | review / low | 4.2 changed (E3 row, new E6), the E1 row did not |

A section-level check alone would have flagged all four. Only two are actually wrong.

## Retrieval

* `GET /generations/{id}`: one generation with its verdict.
* `GET /selections/{id}/generations?status=stale`: everything generated for a selection;
  `status` filters by verdict.
* `GET /nodes/{id}/generations`: matches the **logical** section, so asking with the v2 node of
  3.2 returns test cases generated from v1's 3.2. That is where stale tests are found.
  `node_test_case_ids` says which test cases in each generation cite that section.
  `exact_version=true` limits it to that one node version.

## Limits

1. **One anchor per test case.** A test may depend on a sentence it did not quote. A change there
   gives review/low, not stale. The quote is the model's own claim of what the test checks.
2. **Number detection is token-based.** "two seconds" → "2 seconds" reads as a wording change;
   and a number changed in another row of the same table raises severity for a test whose
   expected result happens to contain the same number.
3. **A changed sentence the quote does not cover** (e.g. a new safety warning added to the
   section) only produces review/low. Severity says how likely the test is wrong, not
   how important the new text is.
4. **Lineage depends on the version matcher.** If the matcher could not link a section across
   versions (see docs/versioning.md), its test cases show the source as removed (stale/high).
   That is safe, because it asks for review, but noisier than necessary.
5. **Staleness is not auto-repaired.** Regeneration is a deliberate `force=true` call, which is
   explicitly out of scope for automation.
