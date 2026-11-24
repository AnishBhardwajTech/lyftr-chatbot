"""Application settings, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default) or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _sqlite_path(url: str) -> str:
    """Turn a SQLAlchemy-style sqlite URL into a filesystem path.

    sqlite:////data/app.db -> /data/app.db   (absolute)
    sqlite:///./app.db     -> ./app.db        (relative)
    A bare path is returned unchanged.
    """
    prefix = "sqlite:///"
    if url.startswith(prefix):
        return url[len(prefix):] or "app.db"
    return url


@dataclass
class Settings:
    api_keys: list[str] = field(default_factory=lambda: _csv("LLM_API_KEYS"))
    # One model for every key, or a comma-separated list aligned with LLM_API_KEYS.
    models: list[str] = field(
        default_factory=lambda: _csv("LLM_MODELS") or _csv("LLM_MODEL", "gemini/gemini-2.5-flash")
    )
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./data/app.db"))
    allowed_origins: list[str] = field(
        default_factory=lambda: _csv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173")
    )
    enable_failure_simulation: bool = field(default_factory=lambda: _bool("ENABLE_FAILURE_SIMULATION"))
    default_system_prompt: str = field(
        default_factory=lambda: os.getenv(
            "DEFAULT_SYSTEM_PROMPT", "You are a helpful, concise assistant."
        )
    )
    cache_type: str = field(default_factory=lambda: os.getenv("CACHE_TYPE", "disk").strip().lower())
    cache_dir: str = field(default_factory=lambda: os.getenv("CACHE_DIR", ""))
    cache_ttl_seconds: int = field(default_factory=lambda: int(os.getenv("CACHE_TTL_SECONDS", "86400")))
    llm_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT_SECONDS", "30")))
    llm_temperature: float = field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.3")))
    llm_max_tokens: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_TOKENS", "1024")))
    max_history_messages: int = field(default_factory=lambda: int(os.getenv("MAX_HISTORY_MESSAGES", "40")))
    # Offline mode for local development / tests: LiteLLM returns a canned reply, no network.
    mock_llm: bool = field(default_factory=lambda: _bool("MOCK_LLM"))

    @property
    def db_path(self) -> str:
        return _sqlite_path(self.database_url)

    def model_for(self, index: int) -> str:
        if not self.models:
            return "gemini/gemini-2.5-flash"
        return self.models[index] if index < len(self.models) else self.models[0]

    @property
    def resolved_cache_dir(self) -> str:
        if self.cache_dir:
            return self.cache_dir
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        return os.path.join(db_dir, "llm_cache")


def get_settings() -> Settings:
    return Settings()
