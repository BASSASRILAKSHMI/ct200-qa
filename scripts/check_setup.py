"""Check that .env is set up: LLM key works, and the generation store is reachable.

    python scripts/check_setup.py

Never prints secrets. Makes one tiny LLM call (a few tokens).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402  (also loads .env)


def check_llm() -> bool:
    from app.llm.client import LLMError, OpenAICompatibleClient
    print(f"LLM     endpoint {config.LLM_BASE_URL}, model {config.LLM_MODEL}")
    if not config.LLM_API_KEY:
        print("  FAIL  no key: put GROQ_API_KEY=... in .env")
        return False
    print(f"        key found ({config.LLM_API_KEY[:4]}…, {len(config.LLM_API_KEY)} chars)")
    try:
        r = OpenAICompatibleClient(max_retries=1).complete(
            [{"role": "user", "content": 'Reply with the JSON object {"ok": true} and nothing else.'}])
        print(f"  OK    replied in {r.latency_ms} ms: {r.text.strip()[:60]}")
        return True
    except LLMError as e:
        print(f"  FAIL  {e}")
        return False


def check_store() -> bool:
    if not config.MONGODB_URI:
        print(f"STORE   MONGODB_URI not set -> JSON files in {os.path.abspath(config.GENERATION_STORE_DIR)}")
        print("  OK    (set MONGODB_URI in .env to use MongoDB Atlas instead)")
        return True
    host = config.MONGODB_URI.split("@")[-1].split("/")[0]
    print(f"STORE   MongoDB at {host}, database {config.MONGODB_DB}")
    try:
        from pymongo import MongoClient
        client = MongoClient(config.MONGODB_URI, serverSelectionTimeoutMS=8000)
        client.admin.command("ping")
        n = client[config.MONGODB_DB]["generations"].count_documents({})
        print(f"  OK    connected; {n} generations stored")
        return True
    except Exception as e:  # noqa: BLE001 - show any connection problem plainly
        msg = str(e).split(",")[0][:200]
        print(f"  FAIL  {type(e).__name__}: {msg}")
        print("        check: password in the URI, Atlas Network Access allows your IP (or 0.0.0.0/0)")
        return False


if __name__ == "__main__":
    ok = check_llm()
    ok = check_store() and ok
    print("\nAll good." if ok else "\nFix the FAIL lines above, then run this again.")
    sys.exit(0 if ok else 1)
