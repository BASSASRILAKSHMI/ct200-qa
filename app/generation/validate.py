"""Validate an LLM reply: structure (Pydantic) and meaning (traceability, grounding).

Leniency policy: only purely MECHANICAL wrapping is repaired (a ```json fence around an
otherwise valid object). Nothing about the CONTENT is repaired or guessed: no dropping a bad
test case to keep the others, no truncating 7 cases to 5, no re-mapping an unknown node id.
Every content problem is reported back to the model, and if retries run out, the generation
is stored as failed with every raw attempt. A partially-correct batch is never accepted,
because "the LLM usually returns 4 good ones" is exactly how unverifiable test cases slip in.

Extra fields the model adds (e.g. "id") are ignored rather than rejected: they cannot cause a
wrong result and rejecting them would only burn retries.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.parsing.normalize import normalize_text
from app.versioning.diff import numbers_in

from .prompt import MAX_CASES, MIN_CASES

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


class TestCase(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    __test__ = False  # not a pytest class

    title: str = Field(min_length=5, max_length=120)
    type: Literal["functional", "boundary", "negative", "safety", "error_handling"]
    priority: Literal["high", "medium", "low"]
    source_node_ids: List[int] = Field(min_length=1)
    requirement_quote: str = Field(min_length=10, max_length=300)
    preconditions: List[str] = Field(default_factory=list)
    steps: List[str] = Field(min_length=1, max_length=15)
    expected_result: str = Field(min_length=5)

    @field_validator("steps", "preconditions")
    @classmethod
    def _no_blank_items(cls, v: List[str]) -> List[str]:
        if any(not str(s).strip() for s in v):
            raise ValueError("must not contain empty strings")
        return v


class TestCaseBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    test_cases: List[TestCase]


@dataclass
class ValidationOutcome:
    ok: bool
    test_cases: List[dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)   # accepted but worth showing (coverage gaps)
    repaired: Optional[str] = None                      # e.g. "stripped markdown fence"


def _canon(text: str) -> str:
    """Normalisation for the quote check: fold case, dash variants and whitespace."""
    t = normalize_text(text).lower()
    t = t.replace("–", "-").replace("—", "-").replace("−", "-")
    t = t.replace("|", " ")      # table rows are stored "E3 | Overpressure | ..."; quotes may span cells
    return re.sub(r"\s+", " ", t).strip(" .")


def _bare_values(text: str) -> set[str]:
    """Numbers/codes in text, reduced to bare values: '±3' -> '3', '15%' -> '15',
    '22–42' -> {'22', '42'}, 'E3' stays 'E3'."""
    out: set[str] = set()
    for tok in numbers_in(text):
        tok = tok.lstrip("±<>≥≤").rstrip("%")
        for part in re.split(r"[–-]", tok):
            if part:
                out.add(part)
    return out


def unverified_values(expected_result: str, cited_text: str) -> list[str]:
    """Values the expected result asserts that the cited sections never state.

    Found on real output: a test anchored to "40 mmHg increments up to a maximum of 299 mmHg"
    expected the cuff to reach "220, 260, then 300 mmHg". The quote was genuine, so grounding
    passed, but 300 contradicts the manual. Derived values (180 + 40 = 220) can be legitimate,
    so this is a WARNING for a human reviewer, not a rejection.
    """
    allowed = _bare_values(cited_text)
    seen: list[str] = []
    for v in sorted(_bare_values(expected_result), key=lambda x: (len(x), x)):
        if v not in allowed and v not in seen:
            seen.append(v)
    return seen


def validate_reply(text: str, finish_reason: Optional[str], sections: list[dict]) -> ValidationOutcome:
    """sections: the pinned sections that were sent, [{"node_id", "number", "title", "body", "explicit"}]."""
    if finish_reason == "length":
        return ValidationOutcome(False, errors=[
            "the reply was cut off before the JSON ended (output too long); write shorter steps"])
    if finish_reason == "json_validate_failed":
        return ValidationOutcome(False, errors=["the reply was not valid JSON (rejected by the provider's JSON mode)"])

    raw = (text or "").strip()
    if not raw:
        return ValidationOutcome(False, errors=["the reply was empty"])

    repaired = None
    m = _FENCE.match(raw)
    if m:
        raw, repaired = m.group(1), "stripped markdown code fence"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return ValidationOutcome(False, errors=[f"the reply is not valid JSON ({e.msg} at line {e.lineno}, column {e.colno})"])
    if not isinstance(data, dict):
        return ValidationOutcome(False, errors=[f'top level must be a JSON object with "test_cases", got {type(data).__name__}'])

    try:
        batch = TestCaseBatch.model_validate(data)
    except ValidationError as e:
        errs = []
        for err in e.errors()[:15]:
            loc = ".".join(str(p) for p in err["loc"])
            errs.append(f"{loc}: {err['msg']}")
        return ValidationOutcome(False, errors=errs)

    errors: list[str] = []
    cases = batch.test_cases
    if not MIN_CASES <= len(cases) <= MAX_CASES:
        errors.append(f"expected {MIN_CASES}-{MAX_CASES} test cases, got {len(cases)}")

    by_id = {s["node_id"]: s for s in sections}
    seen_titles: dict[str, int] = {}
    for i, tc in enumerate(cases, start=1):
        unknown = [nid for nid in tc.source_node_ids if nid not in by_id]
        if unknown:
            errors.append(f"test case {i} cites node ids {unknown} that were not provided "
                          f"(allowed: {sorted(by_id)})")
            continue
        cited_text = " ".join(_canon(f"{by_id[n]['title']} {by_id[n]['body']}") for n in tc.source_node_ids)
        if _canon(tc.requirement_quote) not in cited_text:
            errors.append(f"test case {i} requirement_quote is not copied word for word from its cited "
                          f"section(s) {tc.source_node_ids}: {tc.requirement_quote[:80]!r}")
        key = tc.title.lower()
        if key in seen_titles:
            errors.append(f"test cases {seen_titles[key]} and {i} have the same title")
        seen_titles[key] = i

    if errors:
        return ValidationOutcome(False, errors=errors, repaired=repaired)

    warnings = []
    dumped = []
    for i, tc in enumerate(cases, start=1):
        d = tc.model_dump()
        text = " ".join(f"{by_id[n]['title']} {by_id[n]['body']}" for n in tc.source_node_ids)
        d["unverified_values"] = unverified_values(tc.expected_result, text)
        if d["unverified_values"]:
            warnings.append(f"test case {i} expected result states value(s) not found in its cited "
                            f"section(s): {', '.join(d['unverified_values'])} (check before use)")
        dumped.append(d)

    cited = {n for tc in cases for n in tc.source_node_ids}
    for s in sections:
        if s.get("explicit", True) and s["body"].strip() and s["node_id"] not in cited:
            warnings.append(f"no test case cites selected section {s['number'] or ''} {s['title']} (node {s['node_id']})")

    return ValidationOutcome(
        True,
        test_cases=dumped,
        warnings=warnings,
        repaired=repaired,
    )
