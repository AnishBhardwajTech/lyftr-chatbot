"""LLM gateway: key pool with ordered failover + LiteLLM response cache.

Design
------
* Every key in LLM_API_KEYS becomes an entry in the pool with a stable id (key_1, key_2...).
* A request walks the pool in order. Each attempt is a ``litellm.acompletion`` call with
  that key and ``num_retries=0`` so LiteLLM does not silently retry the same key.
* LiteLLM maps every provider's errors onto one exception hierarchy (RateLimitError,
  InternalServerError, ServiceUnavailableError, ...). Any of them on a key moves the
  request to the next key; when the pool is exhausted we raise AllProvidersUnavailable,
  which the API turns into a clean 503.
* Caching is LiteLLM's built-in cache (``litellm.cache``), disk-backed by default. Its key
  is a hash of the model + full message list (+ sampling params), and never the API key,
  so a cached answer can be served regardless of which key originally produced it.
  LiteLLM marks hits with ``_hidden_params["cache_hit"]``, which we map to LLM_CACHE.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

import litellm
from litellm.caching.caching import Cache

from .config import Settings

log = logging.getLogger("gateway")

SIMULATABLE_STATUSES = {429, 500, 502, 503}


class AllProvidersUnavailable(Exception):
    """Raised when every key in the pool failed for one request."""


@dataclass
class KeyEntry:
    key_id: str
    api_key: str
    model: str


@dataclass
class LLMResult:
    reply: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_hit: bool
    key_id: Optional[str]
    fallback_attempts: int


# ---------------------------------------------------------------------------------------
# Failure simulation (X-Simulate-Failure header)
# ---------------------------------------------------------------------------------------
def parse_simulation_header(raw: Optional[str]) -> dict[str, int]:
    """Parse ``key_1=429,key_2=500`` / ``*=429`` into {"key_1": 429, ...}.

    Malformed pairs and unsupported statuses are ignored rather than failing the request.
    """
    rules: dict[str, int] = {}
    if not raw:
        return rules
    for part in raw.split(","):
        if "=" not in part:
            continue
        key_id, _, status = part.partition("=")
        key_id, status = key_id.strip(), status.strip()
        if not key_id or not status.isdigit():
            continue
        code = int(status)
        if code in SIMULATABLE_STATUSES:
            rules[key_id] = code
    return rules


def simulated_error(status: int, entry: KeyEntry) -> Exception:
    """Build the same exception LiteLLM raises when a provider returns ``status``."""
    provider = entry.model.split("/", 1)[0] if "/" in entry.model else "unknown"
    msg = f"Simulated HTTP {status} for {entry.key_id}"
    if status == 429:
        return litellm.RateLimitError(message=msg, llm_provider=provider, model=entry.model)
    if status == 503:
        return litellm.ServiceUnavailableError(message=msg, llm_provider=provider, model=entry.model)
    if status == 502:
        return litellm.APIError(status_code=502, message=msg, llm_provider=provider, model=entry.model)
    return litellm.InternalServerError(message=msg, llm_provider=provider, model=entry.model)


def describe_error(exc: Exception) -> str:
    """Short, log-safe description: exception type, HTTP status and a trimmed message."""
    status = getattr(exc, "status_code", None)
    kind = type(exc).__name__
    raw = " ".join(str(exc).split())
    # Never let an API key reach the logs (Gemini URLs carry ?key=...).
    raw = re.sub(r"(key=)[^&\s\"']+", r"\1***", raw)
    raw = re.sub(r"\b(AIza[0-9A-Za-z_\-]{10,}|AQ\.[0-9A-Za-z_\-.]{10,}|sk-[0-9A-Za-z_\-]{10,})", "***", raw)
    if "quota" in raw.lower() or "resource_exhausted" in raw.lower():
        kind += "/quota"
    label = f"{kind}({status})" if status else kind
    return f"{label}: {raw[:300]}" if raw else label


# ---------------------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------------------
class LLMGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool = [
            KeyEntry(key_id=f"key_{i + 1}", api_key=key, model=settings.model_for(i))
            for i, key in enumerate(settings.api_keys)
        ]
        if settings.mock_llm and not self.pool:
            # Offline mode with no keys: two fake keys so failover can still be exercised.
            self.pool = [
                KeyEntry("key_1", "mock-key-1", settings.model_for(0)),
                KeyEntry("key_2", "mock-key-2", settings.model_for(1)),
            ]
        self._setup_cache()
        litellm.drop_params = True  # ignore params a given provider does not support
        litellm.suppress_debug_info = True

    def _setup_cache(self) -> None:
        cache_type = self.settings.cache_type
        try:
            if cache_type == "disk":
                litellm.cache = Cache(
                    type="disk", disk_cache_dir=self.settings.resolved_cache_dir, ttl=self.settings.cache_ttl_seconds
                )
            else:
                litellm.cache = Cache(type="local", ttl=self.settings.cache_ttl_seconds)
        except Exception as exc:  # e.g. diskcache missing or directory not writable
            log.warning("Disk cache unavailable (%s); falling back to in-memory cache", exc)
            litellm.cache = Cache(type="local")
            cache_type = "local"
        self.cache_type = cache_type

    @property
    def keys_configured(self) -> int:
        return len(self.pool)

    @property
    def cache_enabled(self) -> bool:
        return litellm.cache is not None

    def _call_kwargs(self, entry: KeyEntry, messages: list[dict[str, str]], use_cache_read: bool) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": entry.model,
            "messages": messages,
            "api_key": entry.api_key,
            "temperature": self.settings.llm_temperature,
            "timeout": self.settings.llm_timeout_seconds,
            "num_retries": 0,
            "caching": True,
        }
        if self.settings.llm_max_tokens > 0:
            kwargs["max_tokens"] = self.settings.llm_max_tokens
        if not use_cache_read:
            # Skip the cache lookup but still store the fresh answer.
            kwargs["cache"] = {"no-cache": True}
        if self.settings.mock_llm:
            last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
            kwargs["mock_response"] = (
                f"[mock reply #{len(messages) // 2}] You said: {last_user[:200]}"
            )
        return kwargs

    async def complete(
        self,
        messages: list[dict[str, str]],
        simulate: Optional[dict[str, int]] = None,
    ) -> LLMResult:
        simulate = simulate or {}
        # While the failover hook is active, read from cache is skipped so the demo
        # actually exercises the keys (a repeated question would otherwise be a cache hit
        # and never reach a provider). Fresh answers are still written to the cache.
        use_cache_read = not simulate
        failures: list[str] = []

        for index, entry in enumerate(self.pool):
            try:
                forced = simulate.get(entry.key_id, simulate.get("*"))
                if forced is not None:
                    raise simulated_error(forced, entry)

                response = await litellm.acompletion(**self._call_kwargs(entry, messages, use_cache_read))
                cache_hit = bool(getattr(response, "_hidden_params", {}).get("cache_hit"))
                reply = (response.choices[0].message.content or "").strip()
                if not reply:
                    raise ValueError("provider returned an empty completion")

                if self.settings.mock_llm and not cache_hit:
                    await asyncio.sleep(0.25)  # mimic network latency so cache hits stand out

                usage = getattr(response, "usage", None)
                input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                if self.settings.mock_llm or not usage or (input_tokens == 0 and output_tokens == 0):
                    # Mock responses carry fixed fake usage, so count tokens locally instead.
                    input_tokens = litellm.token_counter(model=entry.model, messages=messages)
                    output_tokens = litellm.token_counter(model=entry.model, text=reply)

                if failures:
                    log.info("Failover succeeded on %s after: %s", entry.key_id, "; ".join(failures))
                return LLMResult(
                    reply=reply,
                    model=entry.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_hit=cache_hit,
                    key_id=None if cache_hit else entry.key_id,
                    fallback_attempts=0 if cache_hit else index,
                )
            except Exception as exc:  # any provider failure -> try the next key
                failures.append(f"{entry.key_id}: {describe_error(exc)}")
                log.warning("Key %s failed: %s", entry.key_id, describe_error(exc))
                continue

        log.error("All providers unavailable: %s", "; ".join(failures) or "no keys configured")
        raise AllProvidersUnavailable("all providers unavailable")
