"""Stage 3: structural elements -> section tree.

Parent selection is driven by section NUMBERS, not by font size or by "whatever heading came
last". For a heading numbered a.b.c.d the parent is the longest proper prefix that actually
exists in the document so far:

    2.1.1.1  -> 2.1.1 missing -> 2.1 exists -> parent 2.1, flag missing_parent:2.1.1

Irregularities are kept as they appear in the document and FLAGGED on the node, so API
consumers can see them. Nothing is re-sorted or invented.
"""
from __future__ import annotations

import re
from typing import Optional

from .classify import BlockEl, Element, HeadingEl, TitleEl
from .models import Node

ROOT = "doc"


def _parts(number: str) -> tuple[int, ...]:
    return tuple(int(p) for p in number.split("."))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "section"


def build_tree(elements: list[Element]) -> tuple[dict[str, Node], list[str]]:
    nodes: dict[str, Node] = {}
    warnings: list[str] = []
    by_number: dict[tuple[int, ...], str] = {}   # first node seen for each number

    root = Node(key=ROOT, number=None, title="", level=0, parent_key=None, order=0, page=1)
    nodes[ROOT] = root
    current: Node = root

    def add_child(parent: Node, node: Node) -> None:
        node.parent_key = parent.key
        parent.children.append(node.key)
        nodes[node.key] = node

    for el in elements:
        if isinstance(el, TitleEl):
            root.title = el.text
            root.page = el.page
            continue

        if isinstance(el, BlockEl):
            current.blocks.append(el.block)
            continue

        assert isinstance(el, HeadingEl)
        flags = list(el.flags)

        if el.number is None:
            # Unnumbered heading: we cannot know its level from text. Nest it under the
            # current section and say so loudly rather than guessing a level.
            parent = current if current.key != ROOT else root
            key = f"{parent.key}/{_slug(el.title)}"
            n = 2
            while key in nodes:
                key = f"{parent.key}/{_slug(el.title)}-{n}"
                n += 1
            node = Node(key=key, number=None, title=el.title, level=parent.level + 1,
                        parent_key=None, order=len(nodes), page=el.page, flags=flags)
            add_child(parent, node)
            warnings.append(f"unnumbered heading '{el.title}' on page {el.page} nested under {parent.key}")
            current = node
            continue

        parts = _parts(el.number)

        # --- parent: longest existing proper prefix -------------------------
        parent: Node = root
        for cut in range(len(parts) - 1, 0, -1):
            if parts[:cut] in by_number:
                parent = nodes[by_number[parts[:cut]]]
                break
        expected_parent = parts[:-1]
        if expected_parent and (parent.number is None or _parts(parent.number) != expected_parent):
            missing = ".".join(map(str, expected_parent))
            flags.append(f"missing_parent:{missing}")
            warnings.append(
                f"{el.number} '{el.title}': expected parent {missing} not found; "
                f"attached to {parent.number or 'document root'}"
            )

        # --- sibling order ---------------------------------------------------
        siblings = [nodes[k] for k in parent.children if nodes[k].number]
        if siblings:
            prev = _parts(siblings[-1].number)
            if len(prev) == len(parts) and prev[-1] > parts[-1]:
                flags.append(f"out_of_order:after_{siblings[-1].number}")
                warnings.append(f"{el.number} appears after {siblings[-1].number} (document order kept)")

        # --- duplicates ----------------------------------------------------
        key = el.number
        if parts in by_number:
            n = 2
            while f"{el.number}#{n}" in nodes:
                n += 1
            key = f"{el.number}#{n}"
            flags.append("duplicate_number")
            warnings.append(f"section number {el.number} appears more than once; second copy keyed {key}")
        else:
            by_number[parts] = key

        same_title = [n for n in nodes.values() if n.title == el.title and n.key != ROOT]
        if same_title:
            flags.append("duplicate_title")
            for other in same_title:
                if "duplicate_title" not in other.flags:
                    other.flags.append("duplicate_title")
            warnings.append(f"title '{el.title}' used by {', '.join(o.key for o in same_title)} and {key}")

        node = Node(key=key, number=el.number, title=el.title, level=len(parts),
                    parent_key=None, order=len(nodes), page=el.page, flags=flags)
        add_child(parent, node)
        current = node

    return nodes, warnings


def find_parent_chain(nodes: dict[str, Node], key: str) -> list[str]:
    chain: list[str] = []
    cur: Optional[str] = nodes[key].parent_key
    while cur is not None:
        chain.append(cur)
        cur = nodes[cur].parent_key
    return chain
