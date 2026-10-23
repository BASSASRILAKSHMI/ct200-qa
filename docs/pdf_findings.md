# CT-200 PDF inspection findings

Produced with `python scripts/inspect_pdf.py data/ct200_manual.pdf` (and `_v2.pdf`) before
any parser code was written. Each finding lists the evidence and the parsing rule it forced.

## 0. What kind of PDF is this?

* Both files are **digital PDFs with a real text layer** (no scanned pages, no images).
  v1 has 6 pages, v2 has 7. Page size 420 x 595 pt.
* Fonts: `NimbusSans-Bold` for headings, `NimbusSans-Regular` for body, and a stray
  `NotoSerif-Regular` used only for the non-breaking hyphen character.

**Decision:** read the text layer as the primary source (lossless: exact characters, font
names, sizes and coordinates). OCR (Tesseract) is used as a **fallback** for any page with no
text layer, and as an independent **cross-check** that the text layer matches what is
visibly rendered. OCR as the primary source would only add recognition errors here
(e.g. `≥`, `±`, `–` and `CT-200` are classic OCR misreads), and it loses the font
information we need.

## 1. Font size does NOT reliably encode heading level

| Heading | Size | Expected level |
|---|---|---|
| `1. Device Overview` | 16.5 bold | 1 |
| `1.1 Intended Use` | 12.9 bold | 2 |
| `3.2 Cuff Inflation Sequence` | **11.0 bold** | 2 |
| `2.1.1.1 Battery Life Under Typical Use` | 11.0 bold | 4 |

`3.2` is styled like a level-4 heading. A size-based parser would make it a child of `3.1`.
**Rule:** level comes from the section number (`3.2` -> 2 components -> level 2).
Font size is used only as a sanity signal, never to decide level.

## 2. Bold at body size is also used for table headers

`Parameter`, `Value`, `Code`, `Meaning`, `Device Behavior` are 11.0 bold, same as `3.2`.
**Rule:** a heading must be bold **and** at the left margin **and** start with a section
number; lines inside a detected table area are never heading candidates.

## 3. Skipped hierarchy level: `2.1.1.1` directly under `2.1`

There is no `2.1.1`. **Rule:** attach the node to the nearest existing ancestor (`2.1`),
keep its true level (4) and flag it `missing_parent:2.1.1`. We do **not** invent a phantom
`2.1.1` node: that would put text in the tree that is not in the document.

## 4. Out-of-order siblings: `3.4` appears before `3.3`

Document order is 3.1, 3.2, **3.4**, 3.3. **Rule:** keep document order (that is what the
reader sees), parent both under `3`, and flag `3.3` as `out_of_order` (its number is lower
than the previous sibling's). Nothing is re-sorted silently.

## 5. A numbered list that looks exactly like top-level headings

Section 3.3 contains `1. Normal: ...` to `5. Hypertensive Crisis: ...`. These match the
`N. Title` pattern of level-1 headings. A regex-only parser creates five bogus chapters
and re-parents everything after them.
Differences: regular (not bold) font, indented (x = 50.8 instead of 33.0), and item 5 wraps
onto a continuation line at x = 63.0.
**Rule:** these become `list` blocks inside 3.3; continuation lines are joined to their item.

## 6. Duplicate heading titles

`4.2 Error Codes` and `7.1 Error Codes`. **Rule:** node identity is never the title.
Both get distinct node IDs and different parents (`4` and `7`); the duplicate is flagged.

## 7. Headings and paragraphs split across page breaks

* Chapter headings sit at the very bottom of a page with all their content on the next page
  (`2.`, `4.`, `5.`, `7.` in v1).
* Paragraphs break mid-sentence across pages: `3.1` in v1; `4.3`, `6.1`
  ("...and do not" / "use alcohol..."), and `8.1` in v2 (v2 page 7 has only two lines).

**Rule:** parsing runs on one continuous line stream across all pages, not per page. A
paragraph continues across a page break when the previous line does not end with a
sentence terminator.

## 8. Tables with multi-line cells

`2.1 General Specifications` (2 columns) and `4.2 Error Codes` (3 columns). In 4.2 cells wrap
onto 2 to 3 lines (`Cuff not connected or` / `leak detected`). Reading the text layer line by
line interleaves cells from different columns.
**Rule:** detect table regions with PyMuPDF `find_tables()`, extract them as structured rows
(header + cells), and exclude their lines from paragraph parsing.

## 9. Characters that change between versions without any real content change

* Ligatures: `ﬁ`, `ﬀ`, `ﬂ` (`Speciﬁcations`, `Cuﬀ`).
* Non-breaking hyphen U+2011 in `CT‑200`, `non‑volatile`, `self‑calibration`. In v1 it is a
  separate span in a different font; in v2 some are inline. Same visible text, different bytes.

**Rule:** normalise before hashing (NFKC, U+2011 -> `-`, collapse whitespace), otherwise
content hashes report "changed" for text that did not change.

## 10. Numbers that look like headings inside sentences

`(see 2.1, 4.3 for related specifications...)`, `If a code from Section 4.2 appears`.
**Rule:** heading candidates must start the line, be bold and sit at the margin, so these
stay as body text. (They are useful cross-references; storing them is a future extension.)

## 11. Multi-line document title

The title wraps over three 22 pt lines. **Rule:** consecutive title-size lines on page 1 are
merged into one root `document` node.

## What the first implementation got wrong, and how each failure was found

| # | Failure | How it was found | Fix |
|---|---|---|---|
| 1 | Table cells corrupted: `1 .5 seconds`, `0 –299 mmHg`, commas on their own line | Printed PyMuPDF `Table.extract()` output during development and compared it to the raw line dump | Use `find_tables()` only for cell geometry; fill cells from our own text lines |
| 2 | 3.1 split into two paragraphs at a page break | Manual review of the `parse_cli.py` tree: the sentence ended exactly at the page bottom, so the "continue if unfinished sentence" rule broke it | Also continue when the last line on the page is a full-width (wrapped) line |
| 3 | First chapter swallowed as the document title when a PDF has no separate title | Synthetic unit test (`test_duplicate_section_number...`) failed: `1. Intro` was the largest font on page 1 | A line that parses as a numbered heading is never treated as title |
| 4 | Right-edge estimate collapsed on very small inputs | Synthetic page-break test failed with only two body lines | Percentile index rounds up |
| 5 | Table text duplicated on PyMuPDF 1.26–1.28 (passed on 2.0, the version used to develop) | The word-coverage test failed on a second machine: "words in tree that are not in PDF". Reproduced by running the suite in separate virtualenvs per PyMuPDF version; those versions return extra 1-row "tables" nested inside the real ones | Keep only candidates with ≥2 rows and ≥2 columns that are not inside an accepted table; a line can belong to one table only; the parser now warns whenever any line is claimed by two elements. Suite verified on 1.26.7, 1.27.2.3, 1.28.2 and 2.0 |

Lesson from #5: a third-party library's heuristics are part of the input. The parser must
validate what the library returns rather than trust it, and a content-level invariant (every
word exactly once) catches problems that hand-written expected values would miss.

### Validation methods used

1. **Raw layout dump** (`scripts/inspect_pdf.py`): every rule above traces back to a line in this dump.
2. **Word-coverage test**: every word in the PDF's text layer must appear exactly once in the tree
   (titles + numbers + bodies), and no word may appear that is not in the PDF. This catches
   dropped, duplicated and invented content without hand-written expectations.
3. **Independent OCR cross-check** (`scripts/ocr_crosscheck.py`): the OCR pipeline sees only
   pixels and has no font weights, yet reproduces the identical hierarchy (27 sections in v1,
   28 in v2, same titles, parents and flags). Agreement between two unrelated methods is strong
   evidence the hierarchy is right.
4. **Cross-version hash check**: sections whose text did not change must hash identically in
   v1 and v2 even though their page positions, page breaks and hyphen encoding changed.

### Why OCR is a fallback and not the primary source

The cross-check shows what OCR would have introduced on this document:

| Section | Text layer | OCR |
|---|---|---|
| 2.1 | `±3 mmHg` | `+3 mmHg` (wrong accuracy spec) |
| 8.1 | `Class II` | `Class Il` |
| 3.3 | `1. Normal:` | `akWD — . Normal:` (list numbering lost) |
| 1.1, 2.2, 4.3 | `22–42`, `E1–E5` (en dash) | `22—42`, `22-32` (inconsistent dashes) |
| 2.1, 4.2 | structured tables | flattened paragraphs (no cell geometry) |

Every one of these would make an unchanged section look "changed" between versions, and the
`±3` -> `+3` case silently corrupts a medical accuracy specification.

## 12. Figures

Neither PDF contains images (0 image blocks; 170 / 204 vector drawings are table borders), so
no figure or caption needs extracting. The parser still handles them so nothing can be dropped
silently: a placed image becomes a `figure` block in its section, a "Figure N…" line directly
below it becomes the caption, and each figure is reported as a parse warning.

## v1 -> v2 content differences (seen during inspection; used later for versioning)

| Section | Change |
|---|---|
| 2.1.1.1 | 300 -> 250 cycles, 15% -> 10%, extra clause added |
| 3.2 | 40 -> 30 mmHg increments, extra sentence |
| 4.2 | E3 "2 seconds" -> "1.5 seconds"; new row E6 |
| 4.3 | "E1–E5" -> "E1–E6" |
| 5.3 | new section "Data Export" |
| everything else | text identical, but page positions shift |
