"""Coordinate short database operations and external calls without an open transaction."""
from __future__ import annotations

import asyncio
import logging

from app.hr.analyzer import OllamaAnalyzer
from app.hr.service import HRService
from app.max.client import MaxClient, MaxRejected, message_bodies
from app.max.repository import TransportRepository
from app.settings import Settings
from .classifier import Classifier, HttpRagBackend
from .repository import PostgresRepository
from .rules import decide_route
from .schemas import ContextChanged

log = logging.getLogger(__name__)


class BotService:
    def __init__(self, settings: Settings, repository=None, classifier=None, hr=None, rag=None):
        self.settings = settings
        self.repository = repository or PostgresRepository(settings.database_url)
        self.transport = TransportRepository(self.repository)
        self.classifier = classifier or Classifier(model=settings.router_intent_model, base_url=settings.ollama_base_url)
        self.hr = hr or HRService(OllamaAnalyzer(model=settings.ollama_model, base_url=settings.ollama_base_url,
                                               timeout_seconds=settings.ollama_timeout_seconds,
                                               think=None if not settings.ollama_think else settings.ollama_think.lower() == "true"))
        if rag is None:
            from app.rag import PostgresRagBackend
            rag = HttpRagBackend(settings.rag_url) if settings.rag_url else PostgresRagBackend(self.repository)
        self.rag = rag

    async def process_event(self, key=None):
        claim = await asyncio.to_thread(self.repository.claim, key)
        if not claim:
            return None
        try:
            event = self.repository.event_from_claim(claim)
            for _ in range(3):
                context = await asyncio.to_thread(self.repository.context, event)
                decision = decide_route(event, context)
                if decision is None:
                    intent = await self.classifier.classify(event, context)
                    decision = decide_route(event, context, intent)
                try:
                    return await asyncio.to_thread(self.repository.apply, claim, event, context, decision)
                except ContextChanged:
                    continue
            raise ContextChanged("Dialog changed repeatedly")
        except Exception as exc:
            await asyncio.to_thread(self.repository.retry, claim, type(exc).__name__)
            log.warning("Inbox processing deferred: %s", type(exc).__name__)
            return None

    async def analyze_one(self):
        job = await asyncio.to_thread(self.repository.claim_hr)
        if not job:
            return False
        try:
            result = await self.hr.process(job["input"])
            await asyncio.to_thread(self.repository.finish_hr, job, result, self.settings.ollama_model)
        except Exception as exc:
            await asyncio.to_thread(self.repository.fail_hr, job, type(exc).__name__)
            log.warning("HR analysis deferred: %s", type(exc).__name__)
        return True

    async def answer_one(self):
        claimed = await asyncio.to_thread(self.repository.claim_rag)
        if not claimed:
            return False
        job, request = claimed
        try:
            answer = await self.rag.answer(request)
            await asyncio.to_thread(self.repository.finish_rag, job, answer)
        except Exception as exc:
            await asyncio.to_thread(self.repository.fail_rag, job, type(exc).__name__)
            log.warning("Document answer deferred: %s", type(exc).__name__)
        return True

    async def _rate(self, chat_id):
        delay = await asyncio.to_thread(self.transport.reserve_slot, chat_id)
        if delay:
            await asyncio.sleep(delay)

    async def send_one(self, client):
        claimed = await asyncio.to_thread(self.repository.claim_send)
        if not claimed:
            return False
        row, message = claimed
        try:
            parts = await asyncio.to_thread(self.transport.parts, row, message_bodies(message))
            for part in parts:
                if part["external_message_id"]:
                    continue
                await self._rate(message.chat_id)
                mid = await client.send_body(message.chat_id, part["body"])
                # If this commit fails after HTTP success, keep the parent uncertain.
                await asyncio.to_thread(self.transport.part_sent, row, part["part"], mid)
                part["external_message_id"] = mid
            await asyncio.to_thread(self.repository.finish_send, row, parts[0]["external_message_id"])
        except MaxRejected as exc:
            await asyncio.to_thread(self.repository.finish_send, row, error=str(exc), permanent=not exc.retryable)
        except Exception as exc:
            await asyncio.to_thread(self.repository.finish_send, row, error=type(exc).__name__, uncertain=True)
            log.error("Outgoing message needs reconciliation: %s", type(exc).__name__)
        return True

    async def send_transport_one(self, client):
        job = await asyncio.to_thread(self.transport.claim)
        if not job:
            return False
        try:
            await self._rate(job["chat_id"] or "callbacks")
            mid = None
            if job["kind"] == "callback":
                await client.answer_callback(job["callback_id"])
            else:
                mid = await client.send_body(job["chat_id"], job["body"])
            await asyncio.to_thread(self.transport.finish, job, mid)
        except MaxRejected as exc:
            await asyncio.to_thread(self.transport.finish, job, error=str(exc), permanent=not exc.retryable)
        except Exception as exc:
            await asyncio.to_thread(self.transport.finish, job, error=type(exc).__name__, uncertain=True)
        return True

    async def send_batch(self):
        async with MaxClient(self.settings) as client:
            for _ in range(self.settings.worker_batch_size):
                sent_transport = await self.send_transport_one(client)
                sent = await self.send_one(client)
                if not sent and not sent_transport:
                    break

    async def maintenance(self):
        await asyncio.to_thread(self.repository.recover)
        await asyncio.to_thread(self.transport.recover)
        await asyncio.to_thread(self.repository.scheduler_tick)
        await asyncio.to_thread(self.transport.notify_hr)
