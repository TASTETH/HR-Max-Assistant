"""One configuration contract for API, workers and command-line tools."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(default="postgresql://korsik_app:change-me@127.0.0.1:5432/korsik", repr=False)
    database_admin_url: SecretStr | None = None
    database_app_password: SecretStr = SecretStr("change-me")
    redis_url: str = Field(default="redis://127.0.0.1:6379/0", repr=False)
    max_bot_token: SecretStr = SecretStr("")
    max_webhook_secret: SecretStr = SecretStr("")
    max_api_base_url: str = "https://platform-api2.max.ru"
    max_bot_url: str = "https://max.ru/id5021013384_2_bot"
    public_base_url: str = ""
    max_request_timeout: float = Field(default=20, gt=0, le=30)
    max_ca_bundle: str | None = None
    webhook_max_bytes: int = Field(default=65536, ge=4096, le=1048576)
    worker_batch_size: int = Field(default=20, ge=1, le=100)
    local_worker_enabled: bool = False
    router_intent_model: str = ""
    ollama_model: str = "qwen2.5:1.5b"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_timeout_seconds: float = Field(default=120, gt=0, le=240)
    ollama_think: str = ""
    rag_url: str = ""
    hr_max_chat_id: str = ""
    hr_contact_url: str = ""

    @field_validator("max_api_base_url")
    @classmethod
    def secure_api(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("MAX_API_BASE_URL must be an HTTPS API origin")
        return value.rstrip("/")

    @field_validator("public_base_url")
    @classmethod
    def public_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in {None, 443}
                or parsed.query or parsed.fragment or parsed.username):
            raise ValueError("PUBLIC_BASE_URL requires HTTPS on port 443, without credentials/query/fragment")
        return value.rstrip("/")

    def require_webhook_secret(self) -> str:
        import re
        value = self.max_webhook_secret.get_secret_value()
        if not re.fullmatch(r"[A-Za-z0-9_-]{5,256}", value):
            raise ValueError("Set MAX_WEBHOOK_SECRET (5..256 letters, digits, '_' or '-')")
        return value

    @property
    def webhook_url(self) -> str:
        if not self.public_base_url:
            raise ValueError("Set PUBLIC_BASE_URL to your public HTTPS origin")
        return self.public_base_url + "/webhook/max"


@lru_cache
def get_settings() -> Settings:
    return Settings()
