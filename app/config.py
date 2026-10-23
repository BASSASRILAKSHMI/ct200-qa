"""Runtime configuration from environment variables (see .env.example).

A .env file in the project root is loaded automatically if present; real environment
variables take precedence over it.
"""
import os


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# --- relational store (tree, versions, selections) ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ct200.db")
DEFAULT_DOCUMENT_NAME = os.getenv("DEFAULT_DOCUMENT_NAME", "CT-200 Manual")

# --- LLM (any OpenAI-compatible chat-completions endpoint; Groq by default) ---
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_MAX_CONTENT_ATTEMPTS = int(os.getenv("LLM_MAX_CONTENT_ATTEMPTS", "3"))
LLM_MAX_TRANSPORT_RETRIES = int(os.getenv("LLM_MAX_TRANSPORT_RETRIES", "3"))

# --- generated test cases (NoSQL) ---
MONGODB_URI = os.getenv("MONGODB_URI", "")           # empty -> JSON-file store
MONGODB_DB = os.getenv("MONGODB_DB", "ct200_qa")
GENERATION_STORE_DIR = os.getenv("GENERATION_STORE_DIR", "./generation_store")
