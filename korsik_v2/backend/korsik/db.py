"""PostgreSQL access: a small pool, explicit transactions, migrations, job queue."""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import get_settings

log = logging.getLogger(__name__)
_pool: ConnectionPool | None = None
MIGRATIONS = Path(__file__).parent / "migrations"


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(get_settings().database_url, min_size=1, max_size=10,
                               kwargs={"row_factory": dict_row, "autocommit": False}, open=True)
        import atexit
        atexit.register(reset_pool)
    return _pool


def reset_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close(timeout=1)
    _pool = None


@contextmanager
def tx() -> Iterator[psycopg.Connection]:
    """One transaction. Never keep it open across network/LLM calls."""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


def one(sql: str, params: Any = None) -> dict | None:
    with tx() as c:
        return c.execute(sql, params).fetchone()


def all_(sql: str, params: Any = None) -> list[dict]:
    with tx() as c:
        return c.execute(sql, params).fetchall()


def J(value: Any) -> Jsonb:
    return Jsonb(value)


def migrate() -> list[str]:
    applied = []
    with tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        c.execute("SELECT pg_advisory_xact_lock(424242)")
        done = {r["name"] for r in c.execute("SELECT name FROM schema_migrations").fetchall()}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.name in done:
                continue
            c.execute(f.read_text(encoding="utf-8"))
            c.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (f.name,))
            applied.append(f.name)
    return applied


def enqueue(conn: psycopg.Connection, kind: str, payload: dict | None = None, *,
            dedupe_key: str | None = None, run_at=None, request_id: str | None = None) -> int | None:
    row = conn.execute(
        """INSERT INTO jobs(kind, payload, dedupe_key, run_at, request_id)
           VALUES (%s, %s, %s, COALESCE(%s, now()), %s)
           ON CONFLICT (dedupe_key) DO NOTHING RETURNING id""",
        (kind, J(payload or {}), dedupe_key, run_at, request_id or new_request_id())).fetchone()
    conn.execute("NOTIFY korsik_jobs")
    return row["id"] if row else None


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def audit(conn, user_id, action: str, obj: str, details: dict | None = None) -> None:
    conn.execute("INSERT INTO audit_log(user_id, action, object, details) VALUES (%s,%s,%s,%s)",
                 (user_id, action, obj, J(details or {})))


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)
