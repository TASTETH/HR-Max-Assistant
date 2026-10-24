"""ASGI entry point: authenticate, normalize, durably accept, acknowledge."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager, suppress

import psycopg
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from app.max.events import normalize_update
from app.router.repository import PostgresRepository
from app.settings import Settings, get_settings

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, repository=None, enqueue=None) -> FastAPI:
    settings = settings or get_settings()
    secret = settings.require_webhook_secret()
    repository = repository or PostgresRepository(settings.database_url)
    @asynccontextmanager
    async def lifespan(api):
        worker = None
        if settings.local_worker_enabled:
            from app.tasks import local_worker
            worker = asyncio.create_task(local_worker(settings))
        yield
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    api = FastAPI(title="Корсик — MAX webhook", version="1.0.0", lifespan=lifespan)

    @api.get("/health/live")
    async def live():
        return {"status": "ok", "service": "korsik-max"}

    @api.get("/health/ready")
    async def ready():
        try:
            await asyncio.to_thread(repository.ping)
        except psycopg.Error:
            raise HTTPException(503, "PostgreSQL or migrations unavailable") from None
        return {"status": "ready", "service": "korsik-max"}

    @api.post("/webhook/max")
    async def webhook(request: Request):
        provided = request.headers.get("X-Max-Bot-Api-Secret", "")
        if not hmac.compare_digest(provided.encode(), secret.encode()):
            raise HTTPException(401, "Invalid webhook secret")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > settings.webhook_max_bytes:
                raise HTTPException(413, "Webhook payload too large")
        try:
            update = json.loads(raw)
            if not isinstance(update, dict):
                raise ValueError("Expected an update object")
            event = normalize_update(update)
        except (ValueError, TypeError, KeyError, OverflowError, OSError, ValidationError):
            raise HTTPException(400, "Invalid MAX update") from None
        if event is None:
            return {"ok": True, "ignored": True}
        try:
            accepted = await asyncio.to_thread(repository.receive, event)
        except ValueError:
            raise HTTPException(409, "Event identifier conflicts with stored content") from None
        except psycopg.Error:
            log.error("Webhook persistence failed")
            raise HTTPException(503, "Inbox temporarily unavailable") from None
        # Enqueue is only an accelerator: Beat also scans the durable inbox.
        # Do not wait for Redis in the webhook request or fail an accepted event.
        if not accepted.duplicate:
            try:
                if enqueue is not None:
                    enqueue(event.key)
                elif not settings.local_worker_enabled:
                    from app.tasks import process_inbox
                    asyncio.get_running_loop().run_in_executor(None, _enqueue_safely, process_inbox, event.key)
            except Exception:
                log.warning("Inbox accepted; periodic worker will pick it up")
        return {"ok": True, "duplicate": accepted.duplicate}

    return api


def _enqueue_safely(task, key):
    try:
        task.apply_async(args=[key], retry=False)
    except Exception:
        log.warning("Broker unavailable; inbox remains pending")


app = create_app()
