"""SQLAlchemy engine/session setup."""
from __future__ import annotations

from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app import config


class Base(DeclarativeBase):
    pass


def make_engine(url: Optional[str] = None) -> Engine:
    url = url or config.DATABASE_URL
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):
        # SQLite ignores foreign keys unless asked; we rely on them for integrity.
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):  # pragma: no cover - trivial
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(bind: Optional[Engine] = None) -> None:
    from . import models  # noqa: F401  (register tables)
    Base.metadata.create_all(bind=bind or engine)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
