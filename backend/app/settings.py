from functools import lru_cache
from pathlib import Path
import os

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")


class Settings:
    database_url: str = os.getenv("DATABASE_URL", "")
    voter_hash_secret: str = os.getenv("VOTER_HASH_SECRET") or "advertbench-dev-secret"
    app_base_url: str = os.getenv("APP_BASE_URL", "http://localhost:8000")
    data_dir: Path = Path(os.getenv("DATA_DIR", "./data")).resolve()
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    e2b_api_key: str = os.getenv("E2B_API_KEY", "")
    generation_max_turns: int = int(os.getenv("GENERATION_MAX_TURNS", "100"))
    e2b_sandbox_timeout_seconds: int = int(os.getenv("E2B_SANDBOX_TIMEOUT_SECONDS", "3600"))
    e2b_command_timeout_seconds: int = int(os.getenv("E2B_COMMAND_TIMEOUT_SECONDS", "300"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
