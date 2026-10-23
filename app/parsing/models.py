"""Plain data structures shared by the parsing stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class Line:
    """One visual line of text from the PDF (text layer or OCR)."""

    id: int
    page: int           # 1-based
    x0: float
    y0: float
    x1: float
    y1: float
    size: float         # font size in pt (estimated from box height for OCR)
    bold: Optional[bool]  # None when unknown (OCR has no font information)
    text: str
    source: Literal["text", "ocr"] = "text"


@dataclass
class Table:
    """A table region detected on a page. Its lines are consumed by the table, not by paragraphs."""

    page: int
    y0: float
    y1: float
    header: list[str]
    rows: list[list[str]]
    line_ids: list[int] = field(default_factory=list)
    stray_line_ids: list[int] = field(default_factory=list)  # inside the table box but in no cell
    continued: bool = False  # True if merged with a table that started on the previous page


@dataclass
class Block:
    """A content block inside a section."""

    kind: Literal["paragraph", "list", "table"]
    page: int
    text: str = ""                                    # paragraph text
    items: list[str] = field(default_factory=list)    # list items
    header: list[str] = field(default_factory=list)   # table header
    rows: list[list[str]] = field(default_factory=list)  # table rows

    def render(self) -> str:
        """Plain-text form used for the node body, search and hashing."""
        if self.kind == "paragraph":
            return self.text
        if self.kind == "list":
            return "\n".join(self.items)
        lines = [" | ".join(self.header)] if self.header else []
        lines += [" | ".join(r) for r in self.rows]
        return "\n".join(lines)


@dataclass
class Node:
    """A section of the document tree (one per heading, plus the document root)."""

    key: str                    # unique within one parsed version, e.g. "4.2", "doc", "4.2#2"
    number: Optional[str]       # section number as printed, e.g. "2.1.1.1"; None for root/unnumbered
    title: str
    level: int                  # 0 = document root, 1 = "1.", 2 = "1.1", ...
    parent_key: Optional[str]
    order: int                  # position in document order (0 = root)
    page: int
    blocks: list[Block] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "\n\n".join(b.render() for b in self.blocks)

    @property
    def content_hash(self) -> str:
        from .normalize import content_hash
        return content_hash(self.body)


@dataclass
class ParseResult:
    source: str
    page_count: int
    nodes: dict[str, Node]                       # key -> node, insertion = document order
    warnings: list[str] = field(default_factory=list)
    unassigned_lines: list[Line] = field(default_factory=list)
    duplicated_line_ids: list[int] = field(default_factory=list)  # lines claimed by >1 element
    ocr_pages: list[int] = field(default_factory=list)

    @property
    def root(self) -> Node:
        return self.nodes["doc"]

    def by_number(self, number: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.number == number]

    def by_title(self, title: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.title == title]
