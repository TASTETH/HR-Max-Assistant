"""Local Ollama client (native /api/chat and /api/embed). One call per generation.

The token budget is checked in exactly one place (fit_context). Russian text is estimated
conservatively at ~2.2 characters per token for Qwen-family tokenizers.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import httpx

from .config import get_settings

log = logging.getLogger(__name__)
CHARS_PER_TOKEN = 2.2
_sem = None
_sem_lock = threading.Lock()


class LLMError(Exception):
    """Technical failure (timeout, connection, invalid JSON). Not 'no answer in documents'."""


def _semaphore():
    global _sem
    with _sem_lock:
        if _sem is None:
            _sem = threading.BoundedSemaphore(max(1, get_settings().llm_concurrency))
    return _sem


def est_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 4


def budget_for_context(system: str, user_overhead: str) -> int:
    s = get_settings()
    return s.ollama_num_ctx - s.llm_max_answer_tokens - est_tokens(system) - est_tokens(user_overhead) - 64


def chat_json(system: str, user: str, schema: dict, *, request_id: str = "", max_tokens: int | None = None) -> dict:
    s = get_settings()
    total = est_tokens(system) + est_tokens(user) + (max_tokens or s.llm_max_answer_tokens)
    if total > s.ollama_num_ctx:
        raise LLMError(f"context_overflow:{total}>{s.ollama_num_ctx}")
    body = {
        "model": s.ollama_model, "stream": False, "format": schema,
        "think": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"num_ctx": s.ollama_num_ctx, "num_predict": max_tokens or s.llm_max_answer_tokens,
                    "temperature": 0.1},
        "keep_alive": "30m",
    }
    t0 = time.monotonic()
    with _semaphore():
        try:
            r = httpx.post(f"{s.ollama_url}/api/chat", json=body, timeout=s.ollama_timeout)
        except httpx.HTTPError as e:
            raise LLMError(f"ollama_unavailable:{type(e).__name__}") from None
    dt = time.monotonic() - t0
    if r.status_code != 200:
        raise LLMError(f"ollama_http_{r.status_code}")
    try:
        content = r.json()["message"]["content"]
        data = json.loads(content)
    except (KeyError, ValueError, TypeError):
        raise LLMError("invalid_json") from None
    log.info("llm request_id=%s model=%s seconds=%.1f", request_id, s.ollama_model, dt)
    if not isinstance(data, dict):
        raise LLMError("invalid_json")
    return data


def embed(texts: list[str]) -> list[list[float]]:
    s = get_settings()
    if not s.embed_model:
        raise LLMError("embed_disabled")
    out: list[list[float]] = []
    for i in range(0, len(texts), 16):
        try:
            r = httpx.post(f"{s.ollama_url}/api/embed", json={"model": s.embed_model, "input": texts[i:i + 16]},
                           timeout=s.ollama_timeout)
        except httpx.HTTPError as e:
            raise LLMError(f"embed_unavailable:{type(e).__name__}") from None
        if r.status_code != 200:
            raise LLMError(f"embed_http_{r.status_code}")
        out.extend(r.json()["embeddings"])
    return out


def health() -> dict:
    s = get_settings()
    try:
        r = httpx.get(f"{s.ollama_url}/api/tags", timeout=3)
        names = {m["name"] for m in r.json().get("models", [])}
        return {"ok": True, "model": s.ollama_model in names or f"{s.ollama_model}:latest" in names,
                "embed_model": (not s.embed_model) or any(n.split(":")[0] == s.embed_model.split(":")[0] for n in names)}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}
