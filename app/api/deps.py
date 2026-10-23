"""FastAPI dependencies. Tests override these to use a temp DB, an in-memory store and a
scripted LLM, so the suite never needs network access or an API key."""
from app.db.base import get_session
from app.generation.service import LazyLLM
from app.llm.client import LLMClient
from app.storage.generations import GenerationStore
from app.storage.generations import get_store as _get_store


def get_store() -> GenerationStore:
    return _get_store()


def get_llm() -> LLMClient:
    return LazyLLM()


__all__ = ["get_session", "get_store", "get_llm"]
