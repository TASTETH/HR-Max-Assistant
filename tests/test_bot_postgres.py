"""Real PostgreSQL integration. An isolated, randomly named database is created and removed."""
import asyncio
import json
import os
import uuid
from datetime import date
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.cli import approve, invite, migrate, seed
from app.hr.service import HRService
from app.main import create_app
from app.max.client import MaxRejected, MaxUncertain
from app.rag import PostgresRagBackend, import_text
from app.router.handlers import Effects
from app.router.repository import PostgresRepository
from app.router.schemas import IncomingEvent, RagRequest
from app.router.service import BotService
from app.settings import Settings


@pytest.fixture(scope="module")
def db_settings():
    if os.getenv("RUN_POSTGRES_TESTS") != "1":
        pytest.skip("Set RUN_POSTGRES_TESTS=1 for isolated real-PostgreSQL tests")
    local = Settings()
    admin_params = conninfo_to_dict(local.database_admin_url.get_secret_value())
    runtime_params = conninfo_to_dict(local.database_url)
    name = "korsik_test_" + uuid.uuid4().hex[:16]
    maintenance = {**admin_params, "dbname": "postgres"}
    with psycopg.connect(**maintenance, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings = Settings(_env_file=None,
        database_admin_url=make_conninfo(**{**admin_params, "dbname": name}),
        database_url=make_conninfo(**{**runtime_params, "dbname": name}),
        database_app_password=local.database_app_password, max_bot_token="fake", max_webhook_secret="test-secret",
        local_worker_enabled=False, hr_max_chat_id="99999")
    try:
        migrate(settings)
        seed(settings)
        yield settings
    finally:
        assert name.startswith("korsik_test_")
        with psycopg.connect(**maintenance, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


class Analyzer:
    async def analyze(self, data):
        leaving = "об уходе" in data.text.casefold() or "хочу уйти" in data.text.casefold()
        negated = "не хочу" in data.text.casefold()
        return json.dumps({"sentiment": "negative" if leaving and not negated else "neutral", "summary": "Обратная связь сотрудника",
                           "signals": [{"category": "intent_to_leave", "quote": data.text}] if leaving and not negated else []})


class Client:
    def __init__(self):
        self.sent = []

    async def send_body(self, chat_id, body):
        self.sent.append((chat_id, body))
        return "sent-" + uuid.uuid4().hex

    async def answer_callback(self, callback_id):
        pass


def service(settings):
    return BotService(settings, hr=HRService(Analyzer()))


async def accept(bot, account, text="", **kwargs):
    event = IncomingEvent(provider_event_id=uuid.uuid4().hex, account_id=account, chat_id=account, text=text, **kwargs)
    bot.repository.receive(event)
    result = await bot.process_event(event.key)
    assert result and result.status == "done"
    return result


async def employee(settings):
    bot = service(settings)
    account = str(uuid.uuid4().int % 100000000000)
    invitation = invite(settings, site="GrPP")
    token = invitation["invitation_url"].split("?start=", 1)[1]
    await accept(bot, account, "/start " + token)
    await accept(bot, account, "Тестовый сотрудник")
    await accept(bot, account, "+79991234567")
    await accept(bot, account, "yes")
    approve(settings, invitation["employee_id"], "GrPP", "Специалист", date.today())
    bot.repository.scheduler_tick()
    client = Client()
    for _ in range(20):
        if not await bot.send_one(client):
            break
    return bot, account, invitation["employee_id"], client


async def test_webhook_postgres_duplicate_and_unknown_start_are_durable(db_settings):
    bot = service(db_settings)
    payload = {"update_type": "bot_started", "timestamp": 1792830000000,
               "chat_id": 123456, "user": {"user_id": 123456}}
    with TestClient(create_app(db_settings, bot.repository, lambda key: None)) as client:
        headers = {"X-Max-Bot-Api-Secret": "test-secret"}
        assert client.post("/webhook/max", json=payload, headers=headers).status_code == 200
        assert client.post("/webhook/max", json=payload, headers=headers).json()["duplicate"]
    await bot.process_event()
    fake = Client()
    assert await bot.send_transport_one(fake)
    assert "приглашение" in fake.sent[0][1]["text"]


async def test_rating_comment_food_interruption_and_stale_button(db_settings):
    bot, account, eid, fake = await employee(db_settings)
    context = bot.repository.context(IncomingEvent(provider_event_id="context", account_id=account, chat_id=account, text="x"))
    question = context.questions[0]
    result = await accept(bot, account, "7")
    assert result.route == "survey.answer"
    with bot.repository.connection() as conn:
        before = conn.execute("SELECT input_mode FROM korsik_router.profiles WHERE employee_id=%s", (eid,)).fetchone()["input_mode"]
    food = await accept(bot, account, "/Food")
    assert food.route == "content.show"
    with bot.repository.connection() as conn:
        assert conn.execute("SELECT input_mode FROM korsik_router.profiles WHERE employee_id=%s", (eid,)).fetchone()["input_mode"] == before
        action = conn.execute("""SELECT en.data FROM korsik_router.envelopes en JOIN korsik.messages m ON m.id=en.message_id
            WHERE m.employee_id=%s AND m.event_id=%s AND en.data ? 'buttons' ORDER BY m.id LIMIT 1""", (eid, question.id)).fetchone()["data"]["buttons"][0]["callback_data"]
    comment = await accept(bot, account, "Пока разбираюсь с задачами")
    assert comment.route == "survey.comment"
    stale = await accept(bot, account, kind="callback", callback_data=action)
    assert stale.route == "stale_action"
    with bot.repository.connection() as conn:
        row = conn.execute("SELECT status,rating,comment FROM korsik.plan_events WHERE id=%s", (question.id,)).fetchone()
    assert row["status"] == "completed" and row["rating"] == 7 and row["comment"]


async def test_retention_signal_immediately_analyzed_and_hr_notification_deduplicated(db_settings):
    bot, account, eid, fake = await employee(db_settings)
    await accept(bot, account, "9")
    while await bot.send_one(fake):
        pass
    context = bot.repository.context(IncomingEvent(provider_event_id="context", account_id=account, chat_id=account, text="x"))
    assert context.questions[0].settings.kind == "choice"
    result = await accept(bot, account, "Нет, задумываюсь об уходе")
    assert result.route == "survey.answer"
    for _ in range(30):
        if not await bot.analyze_one():
            break
    with bot.repository.connection() as conn:
        rows = conn.execute("SELECT d.priority FROM korsik.hr_cases c JOIN korsik_router.case_details d ON d.case_id=c.id WHERE c.employee_id=%s", (eid,)).fetchall()
    assert [r["priority"] for r in rows] == ["high"]
    bot.transport.notify_hr()
    assert bot.transport.notify_hr() == 0


async def test_rag_sources_and_no_answer_transfer(db_settings, tmp_path):
    bot, account, eid, fake = await employee(db_settings)
    doc = tmp_path / "test_policy.txt"
    doc.write_text("Корпоративный портал. Для получения доступа обратитесь к закреплённому HR.", encoding="utf-8")
    admin = PostgresRepository(db_settings.database_admin_url.get_secret_value())
    version = import_text(admin, doc, title="Тестовый регламент")
    answer = await bot.rag.answer(RagRequest(message_id=1, employee_id=eid, org_unit_id=None, site_code="GrPP",
                                           question="корпоративный портал", allowed_version_ids=[version]))
    assert answer.found and answer.chunk_ids and "Источник: Тестовый регламент" in answer.text
    result = await accept(bot, account, "Как получить космический пропуск?")
    assert result.route == "knowledge.question"
    assert await bot.answer_one()
    with bot.repository.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM korsik.hr_cases WHERE employee_id=%s AND reason='no_answer'", (eid,)).fetchone()["n"] == 1


async def test_timeout_is_not_resent_and_partial_delivery_resumes(db_settings):
    bot, account, eid, fake = await employee(db_settings)
    with bot.repository.connection() as conn:
        Effects(conn, eid, "multipart-test").queue("z" * 8500)
    class Partial(Client):
        async def send_body(self, chat_id, body):
            if len(self.sent) == 1:
                raise MaxRejected("retry", retryable=True)
            return await super().send_body(chat_id, body)
    partial = Partial()
    await bot.send_one(partial)
    assert len(partial.sent) == 1
    with bot.repository.connection() as conn:
        conn.execute("UPDATE korsik.outbox SET next_attempt_at=clock_timestamp() WHERE status='pending'")
    remaining = Client()
    await bot.send_one(remaining)
    assert len(remaining.sent) == 2
    with bot.repository.connection() as conn:
        Effects(conn, eid, "uncertain-test").queue("ambiguous")
    class Ambiguous(Client):
        async def send_body(self, chat_id, body):
            raise MaxUncertain("timeout after write")
    await bot.send_one(Ambiguous())
    assert not await bot.send_one(Client())
    with bot.repository.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM korsik.outbox WHERE status='uncertain'").fetchone()["n"] == 1


async def test_concurrent_inbox_workers_apply_one_effect_and_reminders_are_idempotent(db_settings):
    bot, account, eid, fake = await employee(db_settings)
    event = IncomingEvent(provider_event_id=uuid.uuid4().hex, account_id=account, chat_id=account, text="/HR")
    accepted = await asyncio.gather(asyncio.to_thread(bot.repository.receive, event), asyncio.to_thread(bot.repository.receive, event))
    assert sorted(r.duplicate for r in accepted) == [False, True]
    processed = await asyncio.gather(bot.process_event(event.key), bot.process_event(event.key))
    assert sum(r is not None for r in processed) == 1
    with bot.repository.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM korsik.messages WHERE inbox_event_id=%s", (event.key,)).fetchone()["n"] == 1
        context = bot.repository._context(conn, event)
        question = context.questions[0]
        conn.execute("UPDATE korsik.plan_events SET next_reminder_at=clock_timestamp()-interval '1 second' WHERE id=%s", (question.id,))
    bot.repository.scheduler_tick()
    bot.repository.scheduler_tick()
    with bot.repository.connection() as conn:
        assert conn.execute("SELECT reminders_sent FROM korsik.plan_events WHERE id=%s", (question.id,)).fetchone()["reminders_sent"] == 1
        assert conn.execute("SELECT count(*) AS n FROM korsik.outbox o JOIN korsik.messages m ON m.id=o.message_id WHERE o.purpose='reminder' AND m.event_id=%s", (question.id,)).fetchone()["n"] == 1
