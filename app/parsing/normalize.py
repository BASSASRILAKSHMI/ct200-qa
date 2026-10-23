"""Text normalisation and hashing.

The same visible text can arrive as different bytes (ligatures, U+2011 non-breaking
hyphens split into separate spans, varying whitespace). If we hashed raw text, the v1 -> v2
comparison would flag sections as "changed" when nothing a reader can see changed.
"""
import hashlib
import re
import unicodedata

# Hyphen/dash look-alikes that should compare equal to "-".
# En dash (U+2013) is NOT mapped: "22–42 cm" uses it as a range sign and it is meaningful.
_HYPHENS = {
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen (used in "CT‑200", "non‑volatile")
    "­": "",   # soft hyphen
}
_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Canonical form used for hashing and comparison (not for display)."""
    text = unicodedata.normalize("NFKC", text)  # "ﬁ" -> "fi", "ﬀ" -> "ff"
    for src, dst in _HYPHENS.items():
        text = text.replace(src, dst)
    return _WS.sub(" ", text).strip()


def clean_display_text(text: str) -> str:
    """Lighter clean-up for text we show to users: fix ligatures/hyphens, keep wording."""
    return normalize_text(text)


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
