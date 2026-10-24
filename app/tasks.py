"""Redis is transport; PostgreSQL remains the durable source of work."""
from __future__ import annotations

import asyncio
import logging

from celery import Celery

from app.router.service import BotService
from app.settings import get_settings

settings = get_settings()
celery_app = Celery("korsik", broker=settings.redis_url)
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    task_ignore_result=True, task_acks_late=True, task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True, broker_connection_timeout=1,
    broker_transport_options={"socket_connect_timeout": 1, "socket_timeout": 1, "visibility_timeout": 600},
    task_publish_retry=False, timezone="Europe/Moscow", enable_utc=True,
    beat_schedule={
        "scan-inbox": {"task": "korsik.drain_inbox", "schedule": 2.0},
        "analyze-hr": {"task": "korsik.analyze_hr", "schedule": 2.0},
        "answer-documents": {"task": "korsik.answer_documents", "schedule": 2.0},
        "deliver-outbox": {"task": "korsik.send_outbox", "schedule": 1.0},
        "recover-and-schedule": {"task": "korsik.maintenance", "schedule": 30.0},
    },
)


@celery_app.task(name="korsik.process_inbox")
def process_inbox(key=None):
    result = asyncio.run(BotService(settings).process_event(key))
    return {"status": result.status} if result else None


@celery_app.task(name="korsik.drain_inbox")
def drain_inbox():
    async def run():
        service = BotService(settings)
        for _ in range(settings.worker_batch_size):
            if await service.process_event() is None:
                break
    asyncio.run(run())


@celery_app.task(name="korsik.analyze_hr")
def analyze_hr():
    asyncio.run(BotService(settings).analyze_one())


@celery_app.task(name="korsik.answer_documents")
def answer_documents():
    asyncio.run(BotService(settings).answer_one())


@celery_app.task(name="korsik.send_outbox")
def send_outbox():
    asyncio.run(BotService(settings).send_batch())


@celery_app.task(name="korsik.maintenance")
def maintenance():
    asyncio.run(BotService(settings).maintenance())


async def local_worker(settings):
    """Development runner for Windows without a Redis service. Same durable handlers."""
    log = logging.getLogger(__name__)
    service = BotService(settings)

    async def loop(operation, interval):
        while True:
            try:
                await operation()
            except Exception as exc:
                log.warning("Local worker deferred: %s", type(exc).__name__)
            await asyncio.sleep(interval)

    await asyncio.gather(loop(service.process_event, 0.5), loop(service.analyze_one, 1),
                         loop(service.answer_one, 1), loop(service.send_batch, 0.5),
                         loop(service.maintenance, 15))
