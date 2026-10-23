"""Ingest one or more PDFs, in order, as successive versions of the CT-200 manual.

Usage:
    python scripts/ingest.py data/ct200_manual.pdf                 # creates version 1
    python scripts/ingest.py data/ct200_manual_v2.pdf              # creates version 2 + diff
    python scripts/ingest.py data/ct200_manual.pdf data/ct200_manual_v2.pdf --reset

The database location comes from DATABASE_URL (default sqlite:///./ct200.db).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.db.base import Base, SessionLocal, engine, init_db  # noqa: E402
from app.services.ingestion import IngestionError, ingest_pdf  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--document", default=config.DEFAULT_DOCUMENT_NAME)
    ap.add_argument("--reset", action="store_true", help="drop all tables first")
    args = ap.parse_args()

    if args.reset:
        Base.metadata.drop_all(engine)
    init_db()

    for pdf in args.pdfs:
        with SessionLocal() as session:
            try:
                out = ingest_pdf(session, pdf, document_name=args.document)
            except IngestionError as e:
                print(f"REFUSED {pdf}: {e}")
                return 1
            v = out.version
            if not out.created:
                print(f"{pdf}: identical to latest version {v.version_number}, nothing ingested")
                continue
            print(f"{pdf} -> version {v.version_number} ({len(v.nodes)} nodes, {v.page_count} pages)")
            s = out.summary
            if s.get("previous_version") is None:
                print("  first version of this document")
                continue
            print(f"  compared with version {s['previous_version']}: {s['counts']}")
            for label in ("modified", "added", "moved_or_renamed"):
                if s[label]:
                    print(f"  {label:17}: {', '.join(s[label])}")
            if s["removed"]:
                print(f"  {'removed':17}: {', '.join(r['key'] for r in s['removed'])}")
            for note in s["matcher_notes"]:
                print(f"  note: {note}")
    print(f"\ndatabase: {config.DATABASE_URL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
