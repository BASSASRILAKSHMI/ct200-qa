"""Storage for LLM generations (NoSQL).

Why a document store here while the tree lives in SQLite: a generation is a self-contained,
write-once record with nested, variable-shaped content (test cases, raw attempts, errors).
It is read whole and never joined. The relational side keeps what needs integrity
(versions, parent links, selections).

Each record carries a SNAPSHOT of what it was generated from:
    sources: [{node_id, logical_node_id, version, number, title, content_hash}]
That snapshot is what survives re-versioning: staleness later compares each source's
content_hash with the latest NodeVersion of the same logical_node_id, without ever needing
the old rows to be "current".

Implementations
* MongoGenerationStore  - MongoDB / Atlas (used when MONGODB_URI is set)
* JsonFileGenerationStore - one JSON file per generation; zero setup, fine for a single
  process and hundreds of records; find() scans every file (documented limit)
* InMemoryGenerationStore - tests
Timestamps are stored as ISO-8601 UTC strings in all three, so they behave identically.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Optional, Protocol

from app import config


class GenerationStore(Protocol):
    kind: str

    def insert(self, doc: dict) -> None: ...
    def get(self, gen_id: str) -> Optional[dict]: ...
    def find(
        self,
        selection_id: Optional[int] = None,
        node_id: Optional[int] = None,
        logical_node_id: Optional[int] = None,
        request_key: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]: ...


def _matches(doc: dict, selection_id, node_id, logical_node_id, request_key, status) -> bool:
    if selection_id is not None and doc.get("selection_id") != selection_id:
        return False
    if request_key is not None and doc.get("request_key") != request_key:
        return False
    if status is not None and doc.get("status") != status:
        return False
    srcs = doc.get("sources", [])
    if node_id is not None and not any(s["node_id"] == node_id for s in srcs):
        return False
    if logical_node_id is not None and not any(s["logical_node_id"] == logical_node_id for s in srcs):
        return False
    return True


def _newest_first(docs: list[dict]) -> list[dict]:
    return sorted(docs, key=lambda d: d.get("created_at", ""), reverse=True)


class InMemoryGenerationStore:
    kind = "memory"

    def __init__(self):
        self._docs: dict[str, dict] = {}

    def insert(self, doc: dict) -> None:
        self._docs[doc["_id"]] = json.loads(json.dumps(doc))   # store a copy, like a real DB

    def get(self, gen_id: str) -> Optional[dict]:
        d = self._docs.get(gen_id)
        return json.loads(json.dumps(d)) if d else None

    def find(self, selection_id=None, node_id=None, logical_node_id=None, request_key=None, status=None):
        return _newest_first([json.loads(json.dumps(d)) for d in self._docs.values()
                              if _matches(d, selection_id, node_id, logical_node_id, request_key, status)])


class JsonFileGenerationStore:
    kind = "json-file"

    def __init__(self, directory: str = config.GENERATION_STORE_DIR):
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, gen_id: str) -> str:
        if not gen_id.replace("-", "").isalnum():
            raise ValueError("invalid generation id")
        return os.path.join(self.dir, f"{gen_id}.json")

    def insert(self, doc: dict) -> None:
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path(doc["_id"]))            # atomic: no half-written records

    def get(self, gen_id: str) -> Optional[dict]:
        try:
            with open(self._path(gen_id), encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return None

    def find(self, selection_id=None, node_id=None, logical_node_id=None, request_key=None, status=None):
        out = []
        for name in os.listdir(self.dir):
            if name.endswith(".json"):
                with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                    d = json.load(f)
                if _matches(d, selection_id, node_id, logical_node_id, request_key, status):
                    out.append(d)
        return _newest_first(out)


class MongoGenerationStore:
    kind = "mongodb"

    def __init__(self, uri: str = config.MONGODB_URI, db: str = config.MONGODB_DB, client=None):
        if client is None:
            from pymongo import MongoClient
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        self.col = client[db]["generations"]
        self.col.create_index("selection_id")
        self.col.create_index("request_key")
        self.col.create_index("sources.node_id")
        self.col.create_index("sources.logical_node_id")

    def insert(self, doc: dict) -> None:
        self.col.insert_one(dict(doc))

    def get(self, gen_id: str) -> Optional[dict]:
        return self.col.find_one({"_id": gen_id})

    def find(self, selection_id=None, node_id=None, logical_node_id=None, request_key=None, status=None):
        q: dict = {}
        if selection_id is not None:
            q["selection_id"] = selection_id
        if request_key is not None:
            q["request_key"] = request_key
        if status is not None:
            q["status"] = status
        if node_id is not None:
            q["sources.node_id"] = node_id
        if logical_node_id is not None:
            q["sources.logical_node_id"] = logical_node_id
        return list(self.col.find(q).sort("created_at", -1))


_store: Optional[GenerationStore] = None
_store_lock = threading.Lock()


def get_store() -> GenerationStore:
    """Process-wide store chosen from config: MongoDB if MONGODB_URI is set, else JSON files."""
    global _store
    with _store_lock:
        if _store is None:
            _store = MongoGenerationStore() if config.MONGODB_URI else JsonFileGenerationStore()
        return _store
