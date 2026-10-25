"""Single configuration contract for API, worker and CLI (read from .env)."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]          # project root (contains .env)
BACKEND = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # --- storage
    database_url: str = Field("postgresql://korsik:korsik@127.0.0.1:5432/korsik", repr=False)
    data_dir: Path = ROOT / "data"                    # private uploads, corpus files, caches

    # --- web / auth
    session_hours: int = 12
    cookie_secure: bool = True
    public_base_url: str = ""                           # https://polyplastic.cc.cd

    # --- MAX
    max_bot_token: SecretStr = SecretStr("")
    max_webhook_secret: SecretStr = SecretStr("")
    max_api_base_url: str = "https://platform-api2.max.ru"
    max_bot_url: str = "https://max.ru/id5021013384_2_bot"
    max_ca_bundle: str = ""
    max_timeout: float = 20.0
    max_rate_per_sec: float = 20.0                     # platform limit is 30 rps
    transport: str = "max"                              # max | console

    # --- single HR (resolved by login at runtime)
    hr_login: str = "hr"

    # --- LLM (Ollama native API)
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "poliplastik-qwen:latest"
    ollama_num_ctx: int = 8192
    ollama_timeout: float = 120.0
    llm_max_answer_tokens: int = 700
    llm_concurrency: int = 1
    embed_model: str = "bge-m3"                         # Ollama embedding model; empty = BM25 only

    # --- retrieval
    rag_dense_k: int = 20
    rag_bm25_k: int = 20
    rag_context_k: int = 5
    reranker_model: str = ""                            # optional sentence-transformers cross-encoder

    # --- scenario
    default_timezone: str = "Europe/Moscow"
    preonboarding_days_before: int = 3                  # "за 2-3 дня" -> 3
    default_send_time: str = "10:00"                    # rows without explicit time
    reminder_time: str = "10:00"
    stale_hours: int = 24                               # overdue informational events older than this are skipped
    digest_weekday: int = 0                             # 0=Monday
    digest_time: str = "09:00"
    manager_meeting_rules: str = "survey_problem,probation_end_minus_7"

    def secret(self) -> str:
        value = self.max_webhook_secret.get_secret_value()
        if not re.fullmatch(r"[A-Za-z0-9_-]{5,256}", value):
            raise ValueError("MAX_WEBHOOK_SECRET: 5..256 символов [A-Za-z0-9_-]")
        return value

    @property
    def bot_name(self) -> str:
        return self.max_bot_url.rstrip("/").rsplit("/", 1)[-1]

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "private"

    @property
    def corpus_dir(self) -> Path:
        return self.data_dir / "corpus"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "extract_cache"


@lru_cache
def get_settings() -> Settings:
    return Settings()
