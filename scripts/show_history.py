"""Show every stored version of one section and what changed between them.

Usage:
    python scripts/show_history.py 3.2
    python scripts/show_history.py "Error Codes"     # by title (may match several sections)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import or_, select  # noqa: E402

from app.db.base import SessionLocal, init_db  # noqa: E402
from app.db.models import LogicalNode, NodeVersion  # noqa: E402
from app.versioning.diff import word_diff  # noqa: E402


def main(query: str) -> int:
    init_db()
    with SessionLocal() as s:
        logical_ids = s.scalars(
            select(NodeVersion.logical_node_id)
            .where(or_(NodeVersion.number == query, NodeVersion.title == query))
            .distinct()
        ).all()
        if not logical_ids:
            print(f"no section numbered or titled {query!r}")
            return 1
        for lid in logical_ids:
            ln = s.get(LogicalNode, lid)
            print(f"\nlogical node {lid}")
            prev = None
            for nv in ln.versions:
                print(f"  v{nv.version.version_number}  node_version={nv.id}  {nv.number} {nv.title}"
                      f"  [{nv.change_status}, match={nv.match_method}]  hash={nv.content_hash[:10]}")
                if prev is not None and prev.content_hash != nv.content_hash:
                    d = word_diff(prev.body, nv.body)
                    print(f"      similarity {d['similarity']}")
                    for c in d["changes"]:
                        print(f"      {c['op']:8} '{c['old']}' -> '{c['new']}'   (after: ...{c['context']})")
                    if d["numbers_removed"] or d["numbers_added"]:
                        print(f"      numbers removed {d['numbers_removed']}, added {d['numbers_added']}")
                prev = nv
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "3.2"))
