"""Runtime configuration from environment variables (see .env.example)."""
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ct200.db")
DEFAULT_DOCUMENT_NAME = os.getenv("DEFAULT_DOCUMENT_NAME", "CT-200 Manual")
