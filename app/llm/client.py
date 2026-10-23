"""LLM access behind a tiny interface, so the generation logic never knows the provider.

Two failure families are handled at different layers, on purpose:

* TRANSPORT failures (timeout, connection error, HTTP 429, HTTP 5xx) are retried HERE with
  exponential backoff (honouring Retry-After). They say nothing about the answer's quality.
* AUTH/CONFIG failures (HTTP 400/401/403/404) are NOT retried: retrying cannot fix a bad key or
  an unknown model name. Raised immediately as LLMConfigError.
* CONTENT failures (malformed or invalid answers) are handled one level up, in
  app/generation/service.py, because only that layer knows what a valid answer is.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Union

import httpx

from app import config


class LLMError(Exception):
    """Base class: the LLM call did not produce a response."""


class LLMConfigError(LLMError):
    """Missing/invalid key, unknown model, malformed request. Not retried."""


class LLMUnavailable(LLMError):
    """Transport retries exhausted (rate limited, timeouts, provider errors)."""


@dataclass
class LLMResponse:
    text: str
    model: str
    finish_reason: Optional[str] = None     # "length" means the output was cut off
    latency_ms: int = 0
    usage: dict = field(default_factory=dict)


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, messages: list[dict], json_mode: bool = True) -> LLMResponse: ...


class OpenAICompatibleClient:
    """Chat-completions over plain HTTP. Works with Groq (default), OpenRouter, OpenAI, etc."""

    provider = "openai-compatible"

    def __init__(
        self,
        api_key: str = config.LLM_API_KEY,
        model: str = config.LLM_MODEL,
        base_url: str = config.LLM_BASE_URL,
        timeout: float = config.LLM_TIMEOUT_SECONDS,
        temperature: float = config.LLM_TEMPERATURE,
        max_retries: int = config.LLM_MAX_TRANSPORT_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        transport: Optional[httpx.BaseTransport] = None,   # injectable for tests
    ):
        if not api_key:
            raise LLMConfigError("no LLM API key configured: set GROQ_API_KEY (or LLM_API_KEY) in .env")
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries
        self._sleep = sleep
        self.provider = "groq" if "groq.com" in base_url else base_url
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    def complete(self, messages: list[dict], json_mode: bool = True) -> LLMResponse:
        payload: dict = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                r = self._http.post("/chat/completions", json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = f"{type(e).__name__}: {e}"
                self._backoff(attempt, None)
                continue

            latency = int((time.monotonic() - started) * 1000)
            if r.status_code == 429 or r.status_code >= 500:
                last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                self._backoff(attempt, r.headers.get("retry-after"))
                continue
            if r.status_code >= 400:
                # Groq returns 400 json_validate_failed when its JSON mode rejects the model's
                # own output. That is a CONTENT failure, so surface it as an empty response
                # and let the validation layer retry with feedback.
                if r.status_code == 400 and "json_validate_failed" in r.text:
                    return LLMResponse(text="", model=self.model, finish_reason="json_validate_failed",
                                       latency_ms=latency)
                raise LLMConfigError(f"HTTP {r.status_code} from LLM provider: {r.text[:300]}")

            try:
                body = r.json()
                choice = body["choices"][0]
                return LLMResponse(
                    text=choice["message"].get("content") or "",
                    model=body.get("model", self.model),
                    finish_reason=choice.get("finish_reason"),
                    latency_ms=latency,
                    usage=body.get("usage") or {},
                )
            except (ValueError, KeyError, IndexError, TypeError) as e:
                # provider sent a 200 we cannot read: treat like a transport hiccup
                last_error = f"unreadable provider response ({type(e).__name__}): {r.text[:200]}"
                self._backoff(attempt, None)
                continue

        raise LLMUnavailable(f"LLM call failed after {self.max_retries + 1} attempts: {last_error}")

    def list_models(self) -> list[str]:
        """Model ids this key can use (GET /models). Used by scripts/check_setup.py."""
        r = self._http.get("/models")
        if r.status_code >= 400:
            raise LLMConfigError(f"HTTP {r.status_code} listing models: {r.text[:200]}")
        return sorted(m["id"] for m in r.json().get("data", []))

    def _backoff(self, attempt: int, retry_after: Optional[str]) -> None:
        if attempt >= self.max_retries:
            return
        try:
            delay = float(retry_after) if retry_after else 0.0
        except ValueError:
            delay = 0.0
        self._sleep(min(max(delay, 2 ** attempt), 30.0))   # 1, 2, 4 ... seconds, capped


class ScriptedLLM:
    """Test double: returns pre-written responses in order. An Exception in the script is raised."""

    provider = "scripted"

    def __init__(self, script: list[Union[str, LLMResponse, Exception]], model: str = "scripted-model"):
        self.script = list(script)
        self.model = model
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict], json_mode: bool = True) -> LLMResponse:
        self.calls.append(messages)
        if not self.script:
            raise AssertionError("ScriptedLLM ran out of responses")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, LLMResponse):
            return item
        return LLMResponse(text=item, model=self.model, finish_reason="stop")


def default_client() -> LLMClient:
    return OpenAICompatibleClient()
