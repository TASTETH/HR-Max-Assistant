"""Standalone async router and workers; transport adapters are injected explicitly."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os

import psycopg
from pydantic import ValidationError

from app.hr import HRService
from app.hr.analyzer import SYSTEM_PROMPT
from .classifier import Classifier, HttpRagBackend
from .repository import PostgresRepository
from .rules import decide_route
from .schemas import (ContextChanged, IncomingEvent, RagAnswer, RagBackend,
                      RejectedBeforeAcceptance, RetryLater, RouterResult, Sender)


class Router:
    def __init__(self, repository: PostgresRepository | None = None, *, classifier=None,
                 hr: HRService | None = None, rag: RagBackend | None = None, sender: Sender | None = None):
        self.repository = repository if repository is not None else PostgresRepository()
        self.classifier = classifier if classifier is not None else Classifier()
        self.hr = hr if hr is not None else HRService()
        rag_url = os.getenv("ROUTER_RAG_URL", "") or os.getenv("RAG_URL", "")
        self.rag = rag if rag is not None else (HttpRagBackend(rag_url) if rag_url else None)
        self.sender = sender

    async def handle(self, event: IncomingEvent | dict) -> RouterResult:
        """Accept a verified event. Outgoing previews are already in the durable outbox."""
        event = IncomingEvent.model_validate(event)
        result = await asyncio.to_thread(self.repository.receive, event)
        if result.status in {"done", "dead", "processing"}:
            return result
        handled = await self.process_one(event.key)
        return handled.model_copy(update={"duplicate": result.duplicate}) if handled else result

    async def process_one(self, event_key: str | None = None) -> RouterResult | None:
        claim = await asyncio.to_thread(self.repository.claim, event_key)
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
            raise ContextChanged("Context changed repeatedly")
        except (psycopg.Error, ValidationError, RetryLater, ContextChanged) as exc:
            code = type(exc).__name__
            await asyncio.to_thread(self.repository.retry, claim, code)
            return RouterResult(event_id=claim["event_id"], status="dead" if claim["attempts"] >= 8 else "pending", reason=code)
        except Exception:
            await asyncio.to_thread(self.repository.retry, claim, "ROUTER_FAILURE")
            raise

    def _model_version(self) -> str:
        policy = self.hr.policy
        config = {"high": sorted(policy.high_priority_signals), "auto": sorted(policy.auto_escalate_categories),
                  "low_score": policy.low_score_threshold, "medium": policy.escalate_medium,
                  "prompt": SYSTEM_PROMPT, "think": os.getenv("OLLAMA_THINK", "")}
        digest = hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        model = getattr(self.hr.analyzer, "model", None) or os.getenv("OLLAMA_MODEL", "configured-analyzer")
        return f"{model}:{digest}"

    async def analyze_one(self) -> bool:
        job = await asyncio.to_thread(self.repository.claim_hr)
        if not job:
            return False
        try:
            result = await asyncio.wait_for(self.hr.process(job["input"]), timeout=240)
            await asyncio.to_thread(self.repository.finish_hr, job, result, self._model_version())
        except asyncio.CancelledError:
            await asyncio.to_thread(self.repository.fail_hr, job, "CANCELLED")
            raise
        except Exception as exc:
            await asyncio.to_thread(self.repository.fail_hr, job, type(exc).__name__)
        return True

    async def answer_one(self) -> bool:
        if self.rag is None:
            return False
        claimed = await asyncio.to_thread(self.repository.claim_rag)
        if not claimed:
            return False
        job, request = claimed
        try:
            answer = RagAnswer.model_validate(await asyncio.wait_for(self.rag.answer(request), timeout=240))
            await asyncio.to_thread(self.repository.finish_rag, job, answer)
        except asyncio.CancelledError:
            await asyncio.to_thread(self.repository.fail_rag, job, "CANCELLED")
            raise
        except Exception as exc:
            try:
                await asyncio.to_thread(self.repository.fail_rag, job, type(exc).__name__)
            except psycopg.errors.RaiseException:
                pass
        return True

    async def send_once(self) -> bool:
        if self.sender is None:
            return False
        claimed = await asyncio.to_thread(self.repository.claim_send)
        if not claimed:
            return False
        lease, message = claimed
        try:
            external_id = await asyncio.wait_for(self.sender.send(message), timeout=90)
            if not isinstance(external_id, str) or not external_id.strip():
                raise ValueError("Sender did not return a provider message ID")
        except RejectedBeforeAcceptance:
            await asyncio.to_thread(self.repository.finish_send, lease, error="REJECTED", uncertain=False)
        except asyncio.CancelledError:
            await asyncio.to_thread(self.repository.finish_send, lease, error="CANCELLED", uncertain=True)
            raise
        except Exception:
            await asyncio.to_thread(self.repository.finish_send, lease, error="SEND_RESULT_UNKNOWN", uncertain=True)
        else:
            await asyncio.to_thread(self.repository.finish_send, lease, external_id)
        return True

    async def work_once(self) -> dict:
        recovered = await asyncio.to_thread(self.repository.recover)
        result = await self.process_one()
        analysis = await self.analyze_one()
        rag = await self.answer_one()
        sent = await self.send_once()
        return {"inbox": result.model_dump(mode="json") if result else None, "analysis": analysis,
                "rag": rag, "sent": sent, "rag_configured": self.rag is not None,
                "sender_configured": self.sender is not None, "recovered": recovered}

    async def tick(self, limit: int = 100) -> int:
        return await asyncio.to_thread(self.repository.scheduler_tick, limit)
