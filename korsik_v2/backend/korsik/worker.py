"""Single background process: fast lane (inbound updates, due events, delivery, reminders,
notifications, digest) and a separate heavy lane (LLM/RAG/OCR/corpus builds), so a slow
model never blocks scheduling or message intake. All state lives in PostgreSQL; after a
restart due work is picked up again and unique keys prevent duplicates.

    python -m korsik.worker
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

from .config import get_settings
from .db import migrate, tx
from .logging_setup import setup_logging

log = logging.getLogger("korsik.worker")
HEAVY = {"free_message", "analyze", "hr_doc", "corpus_extract", "corpus_build"}
STOP = threading.Event()


# ---------------------------------------------------------------- fast lane
def process_inbound_batch(limit=20) -> int:
    from .dialog import process_inbound
    with tx() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM inbound_events WHERE status='received' ORDER BY id LIMIT %s",
                                          (limit,)).fetchall()]
    for i in ids:
        try:
            process_inbound(i)
        except Exception as e:
            log.exception("inbound %s failed", i)
            with tx() as c:
                c.execute("UPDATE inbound_events SET status='failed', error=%s, processed_at=now() WHERE id=%s",
                          (f"{type(e).__name__}: {str(e)[:300]}", i))
    return len(ids)


def run_due_events(limit=20, now: datetime | None = None) -> int:
    from .scenario.engine import execute_event, step_by_key, find_material
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    with tx() as c:
        rows = c.execute("""SELECT e.id FROM events e JOIN employees p ON p.id=e.employee_id
                            WHERE e.status='scheduled' AND NOT e.disabled AND e.due_at <= %s
                              AND p.dialog_mode <> 'hr_active' AND p.max_chat_id IS NOT NULL
                              AND (e.step_key='preonboarding' OR e.is_test OR p.status='active')
                              AND (NOT e.needs_permission OR e.permitted_at IS NOT NULL)
                            ORDER BY e.due_at LIMIT %s""", (now, limit)).fetchall()
    n = 0
    for r in rows:
      try:
        with tx() as c:
            ev = c.execute("SELECT * FROM events WHERE id=%s AND status='scheduled' FOR UPDATE SKIP LOCKED",
                           (r["id"],)).fetchone()
            if not ev:
                continue
            emp = c.execute("SELECT * FROM employees WHERE id=%s FOR UPDATE", (ev["employee_id"],)).fetchone()
            if emp["dialog_mode"] == "hr_active":
                continue
            st = step_by_key(ev["step_key"]) or {}
            has_survey = any(a["type"] == "survey" for a in st.get("actions", []))
            # permission-gated events (H34/H36/meeting) may be allowed after their time: never stale
            if (not ev["is_test"] and ev["step_key"] != "preonboarding" and not has_survey
                    and not ev["needs_permission"] and ev["due_at"] < now - timedelta(hours=s.stale_hours)):
                c.execute("UPDATE events SET status='skipped', skip_reason='stale', updated_at=now() WHERE id=%s", (ev["id"],))
                continue
            if st.get("requires_material") and not find_material(c, emp, st["requires_material"]):
                c.execute("UPDATE events SET last_error=%s, updated_at=now() WHERE id=%s",
                          (f"Ожидает материал «{st['requires_material']}»", ev["id"]))
                continue
            execute_event(c, emp, ev, request_id=f"ev{ev['id']}")
            if ev["step_key"] == "preonboarding":
                c.execute("UPDATE employees SET preonboarding_state='done' WHERE id=%s", (emp["id"],))
            n += 1
      except Exception as e:  # one broken event must not stop delivery of everything else
        log.exception("event %s failed", r["id"])
        with tx() as c:
            c.execute("UPDATE events SET status='failed', last_error=%s, attempts=attempts+1, updated_at=now() WHERE id=%s",
                      (f"{type(e).__name__}: {str(e)[:300]}", r["id"]))
    return n


def periodic(state: dict) -> None:
    from .analytics import probation_meetings, weekly
    from .scenario.survey import send_due_reminders
    s = get_settings()
    with tx() as c:
        send_due_reminders(c)
    now = datetime.now(timezone.utc)
    if now - state.get("hourly", now - timedelta(hours=2)) > timedelta(hours=1):
        state["hourly"] = now
        with tx() as c:
            probation_meetings(c)
    local = now.astimezone(ZoneInfo(s.default_timezone))
    h, m = map(int, s.digest_time.split(":"))
    if local.weekday() == s.digest_weekday and (local.hour, local.minute) >= (h, m):
        with tx() as c:
            for u in c.execute("SELECT * FROM users WHERE is_hr_head AND active").fetchall():
                weekly(c, u, now=now)


def fast_loop() -> None:
    from .outbound import send_pending
    state: dict = {}
    conn = psycopg.connect(get_settings().database_url, autocommit=True)
    conn.execute("LISTEN korsik_jobs")
    while not STOP.is_set():
        try:
            busy = process_inbound_batch() + run_due_events() + send_pending()
            periodic(state)
        except Exception:
            log.exception("fast lane iteration failed")
            busy = 0
        if not busy:
            for _ in conn.notifies(timeout=1.0, stop_after=1):
                pass


# ---------------------------------------------------------------- heavy lane
def claim_job(kinds: set[str]) -> dict | None:
    with tx() as c:
        j = c.execute("""SELECT * FROM jobs WHERE status='pending' AND run_at <= now() AND kind = ANY(%s)
                         ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED""", (list(kinds),)).fetchone()
        if j:
            c.execute("UPDATE jobs SET status='running', attempts=attempts+1, locked_until=now()+interval '15 minutes' WHERE id=%s",
                      (j["id"],))
    return j


def run_job(j: dict) -> None:
    from . import corpus, hrdocs
    from .analysis import run_analysis_job
    from .freeflow import handle_free_message
    p, rid = j["payload"], j["request_id"]
    t0 = time.monotonic()
    try:
        if j["kind"] == "free_message":
            handle_free_message(p, rid)
        elif j["kind"] == "analyze":
            run_analysis_job(p, rid)
        elif j["kind"] == "hr_doc":
            hrdocs.process_document(p["document_id"])
        elif j["kind"] == "corpus_extract":
            corpus.process_extract(p["document_id"])
        elif j["kind"] == "corpus_build":
            corpus.run_build(p)
        with tx() as c:
            c.execute("UPDATE jobs SET status='done', finished_at=now() WHERE id=%s", (j["id"],))
        log.info("job %s kind=%s request_id=%s done in %.1fs", j["id"], j["kind"], rid, time.monotonic() - t0)
    except Exception as e:
        log.exception("job %s kind=%s request_id=%s failed", j["id"], j["kind"], rid)
        if j["kind"] == "free_message":
            try:  # the employee's question must not vanish: accept it for manual handling
                from .freeflow import register_failure
                register_failure(p, rid, f"{type(e).__name__}")
            except Exception:
                log.exception("fallback registration failed request_id=%s", rid)
        retry = j["attempts"] < 3 and j["kind"] in ("analyze", "hr_doc", "corpus_extract")
        with tx() as c:
            c.execute("""UPDATE jobs SET status=%s, last_error=%s, run_at=now()+interval '1 minute',
                         finished_at=CASE WHEN %s THEN NULL ELSE now() END WHERE id=%s""",
                      ("pending" if retry else "failed", f"{type(e).__name__}: {str(e)[:300]}", retry, j["id"]))


def heavy_loop() -> None:
    while not STOP.is_set():
        j = claim_job(HEAVY)
        if j:
            run_job(j)
        else:
            STOP.wait(0.5)


def recover() -> None:
    from .outbound import fail_stuck_sending
    fail_stuck_sending()
    with tx() as c:
        c.execute("UPDATE jobs SET status='pending' WHERE status='running'")
        c.execute("UPDATE events SET status='scheduled' WHERE status='sending'")


def main() -> None:
    setup_logging()
    migrate()
    from .scenario.engine import register_scenario
    with tx() as c:
        register_scenario(c)
    recover()
    threads = [threading.Thread(target=fast_loop, name="fast", daemon=True),
               threading.Thread(target=heavy_loop, name="heavy", daemon=True)]
    for t in threads:
        t.start()
    log.info("worker started (transport=%s)", get_settings().transport)
    try:
        while all(t.is_alive() for t in threads):
            time.sleep(2)
    except KeyboardInterrupt:
        STOP.set()


if __name__ == "__main__":
    main()
