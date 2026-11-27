"""Settings loaded from environment variables and the local .env file (environment wins)."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    APP_NAME: str = "AI Box ERP Service"
    HOST: str = "0.0.0.0"
    PORT: int = 8200
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    # Shared secret with the AI_python_backend gateway (AI_BOX_ERP_SERVICE_TOKEN there).
    # Empty disables the check: local development only.
    SERVICE_TOKEN: str = ""

    # The attached ERP report file is the only data source (this service calls no ERP API).
    MAX_UPLOAD_MB: float = 50          # larger files get 413
    MAX_FILE_ROWS: int = 20000         # rows read from one file

    # Request limits.
    MAX_PROMPT_CHARS: int = 2000
    MAX_HISTORY_MONTHS: int = 6

    # Saved reports (Postgres), one owner per report. Empty DATABASE_URL disables saving: reports are
    # still generated, and the /reports endpoints answer 503.
    DATABASE_URL: str = ""
    DB_SCHEMA: str = "ai_box_erp"
    SAVED_PAGE_MAX: int = 50

    # LLM (any OpenAI-compatible chat completions API; DeepSeek by default). Never hard-code the key.
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_MODEL: str = "deepseek-chat"
    LLM_TIMEOUT_SECONDS: float = 180.0
    LLM_MAX_OUTPUT_TOKENS: int = 4096
    LLM_TEMPERATURE: float = 0.2
    AI_SAMPLE_ROWS: int = 25
    # Prices of the LLM per million tokens, used to store each report's cost for billing (report_runs.cost).
    # All 0 = unknown: tokens are still stored and the cost can be computed later. Set them from the
    # provider's price list; the prices used are saved with every run.
    LLM_PRICE_INPUT_PER_MTOK: float = 0.0          # prompt tokens not served from cache
    LLM_PRICE_CACHED_INPUT_PER_MTOK: float = 0.0   # prompt tokens served from the provider's cache
    LLM_PRICE_OUTPUT_PER_MTOK: float = 0.0
    LLM_PRICE_CURRENCY: str = "USD"

    # Reports built at the same time in this process; a request waits at most SLOT_WAIT_SECONDS for a
    # free slot, then gets 429.
    MAX_CONCURRENT_JOBS: int = 2
    SLOT_WAIT_SECONDS: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
