"""Generate QA test cases for a selection: prompt -> LLM -> validate -> retry -> store.

DUPLICATE-SUBMISSION POLICY
    request_key = sha256(selection fingerprint | prompt version | model)
    The fingerprint already encodes the exact node versions AND their content hashes.

    * Same request_key already SUCCEEDED -> return that stored generation (reused=True).
      No LLM call. Reasons: LLM output is non-deterministic, so calling again would give a
      different answer for identical input. For traceability, "the test cases for this exact
      text" should have one answer, and repeat calls cost money and rate limit.
    * force=True -> generate anyway; the new record stores `supersedes` = previous id.
      Nothing is overwritten or deleted, so every earlier result stays auditable.
    * FAILED generations are never reused: retrying a failure must actually retry.
    * Changing the prompt (PROMPT_VERSION) or the model changes the key, so results from a
      different prompt/model are never passed off as current.
    * Concurrency: identical requests in this process are serialised with a per-key lock, so a
      double-click does not cost two LLM calls. Across several server processes two calls could
      still race (documented limit; a unique index on request_key would close it).

RETRY POLICY (content)
    Up to LLM_MAX_CONTENT_ATTEMPTS replies. A rejected reply is sent back with the validator's
    exact errors. If every attempt fails, the generation is stored with status "failed" and all
    raw attempts, and no test cases. Transport retries happen inside the client, separately.
"""
from __future__ import annotations

import hashlib
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app import config
from app.llm.client import LLMClient, LLMConfigError, LLMUnavailable, OpenAICompatibleClient, LLMResponse
from app.services.queries import BadRequest
from app.services.selections import get_selection
from app.storage.generations import GenerationStore

from .prompt import PROMPT_VERSION, build_messages, feedback_message
from .validate import validate_reply

MAX_RAW_CHARS = 20_000          # raw replies are stored for audit, but bounded
_key_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_key_locks_guard = threading.Lock()


class LazyLLM:
    """Knows its provider/model without a key; builds the real client on first use.
    Lets a duplicate request be answered from storage even when no API key is configured."""

    def __init__(self, model: str = config.LLM_MODEL, base_url: str = config.LLM_BASE_URL):
        self.model = model
        self.provider = "groq" if "groq.com" in base_url else base_url
        self._client: Optional[LLMClient] = None

    def complete(self, messages: list[dict], json_mode: bool = True) -> LLMResponse:
        if self._client is None:
            self._client = OpenAICompatibleClient(model=self.model)
        return self._client.complete(messages, json_mode)


def request_key(fingerprint: str, model: str) -> str:
    return hashlib.sha256(f"{fingerprint}|{PROMPT_VERSION}|{model}".encode()).hexdigest()


def _sections(sel) -> list[dict]:
    return [
        {
            "node_id": i.node.id,
            "logical_node_id": i.node.logical_node_id,
            "version": sel.version.version_number,
            "number": i.node.number,
            "title": i.node.title,
            "body": i.node.body,
            "content_hash": i.node.content_hash,
            "explicit": i.explicit,
        }
        for i in sel.items
    ]


def _lock_for(key: str) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks[key]


def generate_for_selection(
    session: Session,
    store: GenerationStore,
    llm: LLMClient,
    selection_id: int,
    force: bool = False,
) -> tuple[dict, bool]:
    """Returns (generation record, reused)."""
    sel = get_selection(session, selection_id)
    sections = _sections(sel)
    if not any(s["body"].strip() for s in sections):
        raise BadRequest("selection contains only headings with no text; "
                         "recreate it with include_descendants=true or pick sections with text")

    key = request_key(sel.fingerprint, llm.model)
    with _lock_for(key):
        previous = store.find(request_key=key, status="succeeded")
        if previous and not force:
            return previous[0], True

        record = _run(llm, sections)
        record.update({
            "_id": uuid.uuid4().hex,
            "request_key": key,
            "selection_id": sel.id,
            "selection_name": sel.name,
            "selection_fingerprint": sel.fingerprint,
            "document_id": sel.document_id,
            "pinned_version": sel.version.version_number,
            "sources": [{k: s[k] for k in ("node_id", "logical_node_id", "version", "number", "title", "content_hash")}
                        for s in sections],
            "prompt_version": PROMPT_VERSION,
            "provider": llm.provider,
            "model": llm.model,
            "forced": force,
            "supersedes": previous[0]["_id"] if (force and previous) else None,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        })
        for n, tc in enumerate(record["test_cases"], start=1):
            tc["id"] = f"TC-{n}"
        store.insert(record)
        return record, False


def _run(llm: LLMClient, sections: list[dict]) -> dict:
    messages = build_messages(sections)
    attempts: list[dict] = []
    for n in range(1, config.LLM_MAX_CONTENT_ATTEMPTS + 1):
        try:
            resp = llm.complete(messages, json_mode=True)
        except LLMConfigError as e:
            return _failed(attempts, "llm_config_error", str(e))
        except LLMUnavailable as e:
            return _failed(attempts, "llm_unavailable", str(e))

        outcome = validate_reply(resp.text, resp.finish_reason, sections)
        attempts.append({
            "attempt": n,
            "finish_reason": resp.finish_reason,
            "latency_ms": resp.latency_ms,
            "usage": resp.usage,
            "raw": (resp.text or "")[:MAX_RAW_CHARS],
            "accepted": outcome.ok,
            "repaired": outcome.repaired,
            "errors": outcome.errors,
        })
        if outcome.ok:
            return {"status": "succeeded", "failure_reason": None, "failure_detail": None,
                    "test_cases": outcome.test_cases, "warnings": outcome.warnings, "attempts": attempts}
        messages = messages + [
            {"role": "assistant", "content": (resp.text or "(empty reply)")[:4000]},
            feedback_message(outcome.errors),
        ]
    return _failed(attempts, "invalid_output",
                   f"no valid reply after {config.LLM_MAX_CONTENT_ATTEMPTS} attempts; last errors: "
                   + "; ".join(attempts[-1]["errors"][:3]))


def _failed(attempts: list[dict], reason: str, detail: str) -> dict:
    return {"status": "failed", "failure_reason": reason, "failure_detail": detail,
            "test_cases": [], "warnings": [], "attempts": attempts}
