"""Transport-level behaviour of the LLM client, using httpx's mock transport (no network)."""
import json

import httpx
import pytest

from app.llm.client import LLMConfigError, LLMUnavailable, OpenAICompatibleClient

OK = {"model": "m", "choices": [{"message": {"content": '{"test_cases": []}'}, "finish_reason": "stop"}],
      "usage": {"total_tokens": 42}}


def client(responses, sleeps=None, **kw):
    seq = list(responses)

    def handler(request):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return OpenAICompatibleClient(api_key="test-key", sleep=(sleeps.append if sleeps is not None else lambda s: None),
                                  transport=httpx.MockTransport(handler), **kw)


def test_requests_json_mode_and_returns_text_usage_and_finish_reason():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=OK)

    c = OpenAICompatibleClient(api_key="test-key", model="llama-x", transport=httpx.MockTransport(handler))
    r = c.complete([{"role": "user", "content": "json please"}])
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["model"] == "llama-x" and seen["auth"] == "Bearer test-key"
    assert r.text == '{"test_cases": []}' and r.finish_reason == "stop" and r.usage["total_tokens"] == 42


def test_rate_limit_and_server_errors_are_retried_with_backoff():
    sleeps = []
    c = client([httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(503), httpx.Response(200, json=OK)],
               sleeps)
    assert c.complete([]).text
    assert sleeps == [3.0, 2]                       # honours Retry-After, then exponential


def test_timeouts_are_retried():
    c = client([httpx.ReadTimeout("slow"), httpx.Response(200, json=OK)])
    assert c.complete([]).finish_reason == "stop"


def test_gives_up_after_retry_budget():
    c = client([httpx.Response(500)] * 3, max_retries=2)
    with pytest.raises(LLMUnavailable, match="after 3 attempts"):
        c.complete([])


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_config_errors_fail_fast_without_retry(code):
    c = client([httpx.Response(code, text="nope")])          # a second call would IndexError
    with pytest.raises(LLMConfigError, match=str(code)):
        c.complete([])


def test_groq_json_mode_rejection_is_a_content_failure_not_a_config_error():
    c = client([httpx.Response(400, json={"error": {"code": "json_validate_failed"}})])
    r = c.complete([])
    assert r.text == "" and r.finish_reason == "json_validate_failed"


def test_unreadable_200_is_retried():
    c = client([httpx.Response(200, text="<html>gateway</html>"), httpx.Response(200, json=OK)])
    assert c.complete([]).text


def test_missing_api_key_is_a_config_error():
    with pytest.raises(LLMConfigError, match="GROQ_API_KEY"):
        OpenAICompatibleClient(api_key="")


def test_list_models_lets_setup_suggest_a_replacement_for_a_retired_model():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}, {"id": "openai/gpt-oss-20b"}]})
        return httpx.Response(404, json={"error": {"code": "model_not_found"}})

    c = OpenAICompatibleClient(api_key="k", model="llama-3.3-70b-versatile", transport=httpx.MockTransport(handler))
    with pytest.raises(LLMConfigError, match="404"):
        c.complete([])                                    # retired model: no retries, clear error
    assert c.list_models() == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
