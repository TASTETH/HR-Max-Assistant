"""Scenario tests run against a real PostgreSQL (TEST_DATABASE_URL), console transport, fake LLM."""
import os
import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://korsik:korsik@127.0.0.1:5432/korsik_test")
os.environ["DATABASE_URL"] = TEST_DB
os.environ["TRANSPORT"] = "console"
os.environ["EMBED_MODEL"] = ""
os.environ["MAX_WEBHOOK_SECRET"] = "test_secret_123"
os.environ["COOKIE_SECURE"] = "false"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    from korsik import config, db, transport
    config.get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    db.reset_pool()
    with psycopg.connect(TEST_DB, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.migrate()
    transport.set_transport(transport.ConsoleTransport(config.get_settings()))
    from korsik.rag import index
    index._cache.clear()
    from korsik.security import hash_password
    with db.tx() as c:
        c.execute("INSERT INTO users(login,password_hash,role,display_name,is_hr_head) VALUES ('hr',%s,'hr','HR Анна',true)",
                  (hash_password("Korsik-HR-2026!"),))
        c.execute("INSERT INTO users(login,password_hash,role,display_name) VALUES ('boss',%s,'manager','Руководитель Петров')",
                  (hash_password("Boss-pass-2026!"),))
        c.execute("INSERT INTO users(login,password_hash,role,display_name) VALUES ('boss2',%s,'manager','Руководитель Сидоров')",
                  (hash_password("Boss-pass-2026!"),))
    yield
    db.reset_pool()


@pytest.fixture
def no_llm(monkeypatch):
    from korsik import analysis
    from korsik.llm import LLMError
    from korsik.rag import answer

    def down(*a, **k):
        raise LLMError("ollama_unavailable:ConnectError")
    monkeypatch.setattr(analysis, "chat_json", down)
    monkeypatch.setattr(answer, "chat_json", down)


# ------------------------------------------------------------------ helpers
def q(sql, params=None):
    from korsik.db import tx
    with tx() as c:
        return c.execute(sql, params).fetchall()


def one(sql, params=None):
    r = q(sql, params)
    return r[0] if r else None


def make_employee(**kw):
    from korsik.db import tx
    data = dict(full_name="Иванов Иван Иванович", short_name="Иван", organization="ООО «Группа ПОЛИПЛАСТИК»",
                department="Финансы", position="Аналитик", tags=["GrPP"], start_date=date.today() + timedelta(days=3),
                timezone="Europe/Moscow", work_format="office")
    data.update(kw)
    with tx() as c:
        hr = c.execute("SELECT id FROM users WHERE login='hr'").fetchone()
        cols = list(data)
        return c.execute(f"INSERT INTO employees({','.join(cols)}, hr_user_id) VALUES ({','.join(['%s'] * len(cols))}, %s) RETURNING id",
                         [*data.values(), hr["id"]]).fetchone()["id"]


def invite(emp_id):
    from korsik.db import tx
    from korsik.dialog import token_hash
    tok = uuid.uuid4().hex
    with tx() as c:
        c.execute("INSERT INTO invites(kind, employee_id, token_hash, created_by, expires_at) VALUES ('employee',%s,%s,1,%s)",
                  (emp_id, token_hash(tok), datetime.now(timezone.utc) + timedelta(days=3)))
    return tok


def push(update):
    from korsik.db import J, tx
    from korsik.dialog import provider_event_id
    with tx() as c:
        c.execute("INSERT INTO inbound_events(provider_event_id, payload, request_id) VALUES (%s,%s,'t') ON CONFLICT DO NOTHING",
                  (provider_event_id(update), J(update)))
    pump()


def start(user, payload, chat=None):
    push({"update_type": "bot_started", "timestamp": __import__("time").time_ns(), "chat_id": chat or f"chat-{user}", "user": {"user_id": user},
          "payload": payload})


def say(user, text, chat=None):
    push({"update_type": "message_created", "timestamp": 1,
          "message": {"recipient": {"chat_id": chat or f"chat-{user}", "chat_type": "dialog"}, "sender": {"user_id": user},
                      "body": {"mid": uuid.uuid4().hex, "text": text}}})


def press(user, payload, chat=None):
    push({"update_type": "message_callback", "timestamp": 1,
          "callback": {"callback_id": uuid.uuid4().hex, "payload": payload, "user": {"user_id": user}},
          "message": {"recipient": {"chat_id": chat or f"chat-{user}", "chat_type": "dialog"}}})


def pump(now=None, jobs=True):
    """Run worker steps synchronously until idle."""
    from korsik import worker
    from korsik.outbound import send_pending
    for _ in range(20):
        n = worker.process_inbound_batch() + worker.run_due_events(now=now) + send_pending()
        if jobs:
            j = worker.claim_job(worker.HEAVY)
            if j:
                worker.run_job(j)
                n += 1
        if not n:
            break


def out_texts(emp_id, since_id=0):
    return [r["text"] for r in q("SELECT text FROM messages WHERE employee_id=%s AND direction='out' AND id>%s ORDER BY id",
                                 (emp_id, since_id))]


def last_msg_id(emp_id):
    r = one("SELECT max(id) m FROM messages WHERE employee_id=%s", (emp_id,))
    return r["m"] or 0


def buttons_of_last(emp_id):
    r = one("""SELECT o.body FROM outbox o WHERE o.employee_id=%s AND jsonb_array_length(o.body->'buttons')>0
               ORDER BY o.id DESC LIMIT 1""", (emp_id,))
    return [b for row in r["body"]["buttons"] for b in row] if r else []
