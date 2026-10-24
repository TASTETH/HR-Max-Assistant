"""Short PostgreSQL transactions; the service runs these operations in worker threads."""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.hr.schemas import HRResult
from .schemas import (Action, Context, ContextChanged, IncomingEvent, InvalidAction,
                      OutgoingMessage, Question, QuestionSettings, RagAnswer, RagRequest,
                      RetryLater, RouterResult)


class PostgresRepository:
    def __init__(self, dsn: str | None = None):
        self._dsn = dsn or os.getenv("DATABASE_URL", "")
        if not self._dsn:
            raise ValueError("Set DATABASE_URL to the installed korsik PostgreSQL database")

    @contextmanager
    def connection(self):
        with psycopg.connect(self._dsn, row_factory=dict_row, connect_timeout=5) as conn:
            conn.execute("SET LOCAL search_path=korsik,pg_catalog")
            conn.execute("SET LOCAL statement_timeout='15s'")
            conn.execute("SET LOCAL lock_timeout='3s'")
            yield conn

    def install(self, role: str | None = None):
        """Explicit owner/migrator operation; never run automatically on an app request."""
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            if role:
                conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            conn.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"), prepare=False)

    def receive(self, event: IncomingEvent) -> RouterResult:
        payload = event.envelope()
        payload["router_version"] = 1
        with self.connection() as conn:
            inserted = conn.execute("""INSERT INTO korsik.max_inbox(event_id,payload)
                VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING event_id""", (event.key, Jsonb(payload))).fetchone()
            row = conn.execute("SELECT * FROM korsik.max_inbox WHERE event_id=%s", (event.key,)).fetchone()
            original, repeated = dict(row["payload"]), dict(payload)
            original.pop("occurred_at", None)
            repeated.pop("occurred_at", None)
            if original != repeated:
                raise ValueError("Provider event ID was reused with different content")
            if event.callback_id and self._has_table(conn, "korsik_router.transport_outbox"):
                from app.max.repository import TransportRepository
                TransportRepository.queue(conn, f"callback:{event.callback_id}", callback_id=event.callback_id, chat_id=event.chat_id)
            return self._result(row, duplicate=not bool(inserted))

    def ping(self):
        with self.connection() as conn:
            conn.execute("SELECT id FROM korsik_router.transport_outbox LIMIT 0")

    @staticmethod
    def _has_table(conn, name: str) -> bool:
        return conn.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (name,)).fetchone()["present"]

    @staticmethod
    def _result(row, *, duplicate=False):
        if row["result"]:
            return RouterResult.model_validate({**row["result"], "duplicate": duplicate})
        return RouterResult(event_id=row["event_id"], status=row["status"], duplicate=duplicate)

    def result(self, event_key: str) -> RouterResult | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM korsik.max_inbox WHERE event_id=%s AND payload->>'router_version'='1'", (event_key,)).fetchone()
            return self._result(row) if row else None

    def claim(self, event_key: str | None = None):
        with self.connection() as conn:
            return conn.execute("""WITH picked AS (
                SELECT event_id FROM korsik.max_inbox
                WHERE status='pending' AND next_attempt_at<=clock_timestamp()
                  AND payload->>'router_version'='1' AND (%s::text IS NULL OR event_id=%s)
                ORDER BY received_at,event_id LIMIT 1 FOR UPDATE SKIP LOCKED
            ) UPDATE korsik.max_inbox i SET status='processing',attempts=i.attempts+1,
                generation=i.generation+1,lease_until=clock_timestamp()+interval '2 minutes'
              FROM picked p WHERE i.event_id=p.event_id RETURNING i.*""", (event_key, event_key)).fetchone()

    @staticmethod
    def event_from_claim(claim) -> IncomingEvent:
        return IncomingEvent.model_validate({k: v for k, v in claim["payload"].items() if k != "router_version"})

    def context(self, event: IncomingEvent) -> Context:
        with self.connection() as conn:
            return self._context(conn, event)

    @staticmethod
    def _context(conn, event: IncomingEvent, *, lock=False) -> Context:
        if event.kind in {"invitation", "delivery"}:
            return Context()
        employee = conn.execute("""SELECT * FROM korsik.employees
            WHERE max_account_id=%s AND max_chat_id=%s""" + (" FOR UPDATE" if lock else ""),
            (event.account_id, event.chat_id)).fetchone()
        if not employee:
            return Context()
        eid = employee["id"]
        profile = conn.execute("SELECT * FROM korsik_router.profiles WHERE employee_id=%s", (eid,)).fetchone() or {}
        questions = []
        rows = conn.execute("""SELECT e.*,c.config,a.revision answer_revision,a.stage answer_stage,
               a.selected,a.score,a.root_message_id
            FROM korsik.plan_events e JOIN korsik.employee_plans p ON p.id=e.plan_id
            LEFT JOIN korsik_router.question_configs c ON c.event_id=e.id
            LEFT JOIN korsik_router.answers a ON a.event_id=e.id
            WHERE e.employee_id=%s AND p.status='active'
              AND e.status IN ('queued','awaiting_answer','awaiting_comment') AND e.kind<>'info'
            ORDER BY e.position,e.id""", (eid,)).fetchall()
        for row in rows:
            settings = QuestionSettings.model_validate(row["config"] or {
                "kind": row["kind"], "survey_id": str(row["plan_id"]), "prompt": row["body"]})
            questions.append(Question(id=row["id"], plan_id=row["plan_id"], status=row["status"],
                deadline_at=row["deadline_at"], settings=settings, revision=row["answer_revision"] or 0,
                stage=row["answer_stage"] or ("awaiting_comment" if row["status"] == "awaiting_comment" else "draft"),
                selected=row["selected"] or [], score=row["score"] or row["rating"], root_message_id=row["root_message_id"]))
        reply = None
        if event.reply_to_message_id:
            if PostgresRepository._has_table(conn, "korsik_router.delivery_parts"):
                reply = conn.execute("""SELECT m.event_id,m.case_id FROM korsik.outbox o
                    JOIN korsik.messages m ON m.id=o.message_id
                    WHERE (o.external_message_id=%s OR EXISTS(SELECT 1 FROM korsik_router.delivery_parts dp
                      WHERE dp.message_id=m.id AND dp.external_message_id=%s)) AND m.employee_id=%s""",
                    (event.reply_to_message_id, event.reply_to_message_id, eid)).fetchone()
            else:
                reply = conn.execute("""SELECT m.event_id,m.case_id FROM korsik.outbox o JOIN korsik.messages m ON m.id=o.message_id
                    WHERE o.external_message_id=%s AND m.employee_id=%s""", (event.reply_to_message_id, eid)).fetchone()
        action = None
        invalid = False
        if event.kind == "callback":
            row = conn.execute("""SELECT action FROM korsik_router.actions
                WHERE token_hash=%s AND employee_id=%s AND expires_at>clock_timestamp()""",
                (hashlib.sha256(event.callback_data.encode()).digest(), eid)).fetchone()
            action = Action.model_validate(row["action"]) if row else None
            invalid = not bool(action)
        cases = conn.execute("SELECT id FROM korsik.hr_cases WHERE employee_id=%s AND status='awaiting_hr' ORDER BY id", (eid,)).fetchall()
        return Context(employee_id=eid, hr_id=employee["hr_id"], lifecycle=employee["lifecycle"],
            registration_step=employee["registration_step"], registration_revision=employee["registration_revision"],
            org_unit_id=employee["org_unit_id"], site_code=profile.get("site_code"),
            preferred_address=profile.get("preferred_address", "formal"), profile_revision=profile.get("revision", 0),
            input_mode=profile.get("input_mode", {}), questions=questions,
            open_cases=[r["id"] for r in cases], action=action, action_invalid=invalid,
            reply_known=bool(reply), reply_question_id=reply["event_id"] if reply else None,
            reply_case_id=reply["case_id"] if reply else None)

    def apply(self, claim, event: IncomingEvent, expected: Context, decision) -> RouterResult:
        from .handlers import Handlers
        with self.connection() as conn:
            inbox = conn.execute("SELECT * FROM korsik.max_inbox WHERE event_id=%s FOR UPDATE", (claim["event_id"],)).fetchone()
            if not inbox or inbox["status"] != "processing" or inbox["generation"] != claim["generation"]:
                raise ContextChanged("Inbox ownership changed")
            fresh = self._context(conn, event, lock=True)
            if fresh.fingerprint != expected.fingerprint:
                raise ContextChanged("Dialog changed before applying the route")
            handler = Handlers(conn, event, fresh, decision)
            try:
                with conn.transaction():
                    result = handler.dispatch()
            except InvalidAction as exc:
                handler = Handlers(conn, event, fresh, decision)
                result = handler.invalid(str(exc))
            if result.notice and not result.outgoing and event.chat_id and self._has_table(conn, "korsik_router.transport_outbox"):
                from app.max.repository import TransportRepository
                TransportRepository.queue(conn, f"notice:{event.key}", chat_id=event.chat_id,
                    text="Здравствуйте! Я Корсик, помощник по адаптации. " + result.notice)
            conn.execute("""UPDATE korsik.max_inbox SET employee_id=%s,status='done',lease_until=NULL,
                result=%s,last_error=NULL WHERE event_id=%s""",
                (result.employee_id, Jsonb(result.model_dump(mode="json")), claim["event_id"]))
            return result

    def retry(self, claim, code: str):
        with self.connection() as conn:
            conn.execute("""UPDATE korsik.max_inbox SET status=CASE WHEN attempts>=8 THEN 'dead' ELSE 'pending' END,
                lease_until=NULL,last_error=%s,next_attempt_at=clock_timestamp()+interval '10 seconds'
                WHERE event_id=%s AND status='processing' AND generation=%s""",
                (code[:100], claim["event_id"], claim["generation"]))

    def recover(self):
        with self.connection() as conn:
            state = conn.execute("SELECT * FROM korsik.recover_leases()").fetchone()
            rag = conn.execute("SELECT korsik.recover_rag_leases() AS recovered").fetchone()
            count = conn.execute("""UPDATE korsik_router.hr_jobs SET
                status=CASE WHEN attempts>=8 THEN 'dead' ELSE 'pending' END,lease_until=NULL,
                next_attempt_at=clock_timestamp(),last_error='LEASE_EXPIRED'
                WHERE status='running' AND lease_until<clock_timestamp() RETURNING id""").fetchall()
            return {**state, "rag_recovered": rag["recovered"], "hr_recovered": len(count)}

    def claim_hr(self):
        with self.connection() as conn:
            return conn.execute("""WITH picked AS (
                SELECT id FROM korsik_router.hr_jobs WHERE status='pending' AND next_attempt_at<=clock_timestamp()
                ORDER BY next_attempt_at,id LIMIT 1 FOR UPDATE SKIP LOCKED
            ) UPDATE korsik_router.hr_jobs j SET status='running',attempts=j.attempts+1,
                generation=j.generation+1,lease_until=clock_timestamp()+interval '5 minutes'
              FROM picked p WHERE j.id=p.id RETURNING j.*""").fetchone()

    def finish_hr(self, job, result: HRResult, model_version: str):
        from .handlers import Effects
        with self.connection() as conn:
            conn.execute("SELECT id FROM korsik.employees WHERE id=%s FOR UPDATE", (job["employee_id"],))
            current = conn.execute("SELECT * FROM korsik_router.hr_jobs WHERE id=%s FOR UPDATE", (job["id"],)).fetchone()
            if current["status"] != "running" or current["generation"] != job["generation"]:
                raise ContextChanged("HR job ownership changed")
            if result.event_id != job["input"]["event_id"] or result.employee_id != str(job["employee_id"]):
                raise ValueError("HR result does not belong to the claimed job")
            conn.execute("""INSERT INTO korsik_router.hr_results(job_id,generation,model_version,result)
                VALUES(%s,%s,%s,%s)""", (job["id"], job["generation"], model_version, Jsonb(result.model_dump(mode="json"))))
            effects = Effects(conn, job["employee_id"], f"hr-job:{job['id']}")
            if result.ticket:
                effects.open_case(job["root_message_id"], "problem" if job["input"]["source"] != "hr_request" else "explicit_human",
                                  result.analysis.summary if result.analysis else result.ticket.description,
                                  result.ticket.priority, result.ticket.category, job["message_id"])
            if result.status == "ok":
                a = result.analysis
                conn.execute("""INSERT INTO korsik.message_analysis
                    (message_id,model_version,topic,sentiment,problem_level)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (job["message_id"], model_version, a.signals[0].category if a.signals else "general",
                     a.sentiment, {"low": 0, "medium": 2, "high": 3}[a.priority]))
            if result.status == "error":
                conn.execute("""UPDATE korsik_router.hr_jobs SET
                    status=CASE WHEN attempts>=8 THEN 'dead' ELSE 'pending' END,lease_until=NULL,
                    last_error=%s,next_attempt_at=clock_timestamp()+interval '1 minute' WHERE id=%s""",
                    (result.error.code, job["id"]))
            else:
                if job["continue_with_rag"] and not result.ticket:
                    conn.execute("""UPDATE korsik.messages SET rag_status='pending',rag_next_attempt_at=clock_timestamp()
                        WHERE id=%s AND case_id IS NULL AND rag_status='none'""", (job["message_id"],))
                conn.execute("UPDATE korsik_router.hr_jobs SET status='done',lease_until=NULL,last_error=NULL WHERE id=%s", (job["id"],))

    def fail_hr(self, job, code: str):
        with self.connection() as conn:
            conn.execute("""UPDATE korsik_router.hr_jobs SET
                status=CASE WHEN attempts>=8 THEN 'dead' ELSE 'pending' END,lease_until=NULL,
                last_error=%s,next_attempt_at=clock_timestamp()+interval '1 minute'
                WHERE id=%s AND status='running' AND generation=%s""", (code[:100], job["id"], job["generation"]))

    def claim_rag(self):
        with self.connection() as conn:
            job = conn.execute("SELECT * FROM korsik.claim_rag_question()").fetchone()
            if not job:
                return None
            employee = conn.execute("""SELECT e.org_unit_id,p.site_code FROM korsik.employees e
                LEFT JOIN korsik_router.profiles p ON p.employee_id=e.id WHERE e.id=%s""", (job["employee_id"],)).fetchone()
            versions = conn.execute("""SELECT v.id FROM korsik.document_versions v JOIN korsik.documents d ON d.id=v.document_id
                WHERE d.enabled AND v.is_current AND v.index_status='ready' AND v.valid_from<=clock_timestamp()
                  AND (v.valid_until IS NULL OR v.valid_until>clock_timestamp())
                  AND (d.scope_org_unit_id IS NULL OR d.scope_org_unit_id=%s) ORDER BY v.id""", (employee["org_unit_id"],)).fetchall()
            request = RagRequest(message_id=job["id"], employee_id=job["employee_id"],
                org_unit_id=employee["org_unit_id"], site_code=employee["site_code"], question=job["body"],
                allowed_version_ids=[v["id"] for v in versions])
            return job, request

    def finish_rag(self, job, answer: RagAnswer):
        from .handlers import Effects
        with self.connection() as conn:
            conn.execute("""SELECT korsik.queue_rag_answer(%s,%s,%s::bigint[],%s,%s,%s)""",
                (job["id"], answer.text, answer.chunk_ids, answer.model_version, answer.found, job["rag_generation"]))
            if not answer.found:
                Effects(conn, job["employee_id"], f"rag:{job['id']}").open_case(
                    job["id"], "no_answer", "В доступных корпоративных материалах не найден подтверждённый ответ.")

    def fail_rag(self, job, code: str):
        with self.connection() as conn:
            conn.execute("SELECT korsik.fail_rag_question(%s,%s,%s)", (job["id"], job["rag_generation"], code[:100]))

    @staticmethod
    def outgoing(conn, ids: list[int]) -> list[OutgoingMessage]:
        if not ids:
            return []
        rows = conn.execute("""SELECT m.id,m.body,e.max_chat_id,en.data FROM korsik.messages m
            JOIN korsik.employees e ON e.id=m.employee_id
            LEFT JOIN korsik_router.envelopes en ON en.message_id=m.id
            WHERE m.id=ANY(%s::bigint[]) ORDER BY m.id""", (ids,)).fetchall()
        return [OutgoingMessage(message_id=r["id"], chat_id=r["max_chat_id"], text=r["body"],
            buttons=(r["data"] or {}).get("buttons", []), attachments=(r["data"] or {}).get("attachments", [])) for r in rows]

    def claim_send(self):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM korsik.claim_outbox(1)").fetchone()
            return (row, self.outgoing(conn, [row["message_id"]])[0]) if row else None

    def finish_send(self, row, external_id=None, *, error=None, uncertain=False, permanent=False):
        with self.connection() as conn:
            conn.execute("SELECT korsik.finish_send(%s,%s,%s,%s,%s)",
                (row["message_id"], row["generation"], external_id, error, uncertain))
            if permanent:
                conn.execute("UPDATE korsik.outbox SET status='dead' WHERE message_id=%s AND generation=%s AND status='pending'",
                             (row["message_id"], row["generation"]))

    def configure_question(self, authenticated_hr_id: int, event_id: int, settings: QuestionSettings):
        settings = QuestionSettings.model_validate(settings)
        with self.connection() as conn:
            row = conn.execute("""SELECT e.id,e.plan_id,e.kind,e.position FROM korsik.plan_events e
                JOIN korsik.employees p ON p.id=e.employee_id JOIN korsik.hr_users h ON h.id=p.hr_id
                WHERE e.id=%s AND p.hr_id=%s AND h.enabled AND e.status='scheduled' FOR UPDATE OF p,e""",
                (event_id, authenticated_hr_id)).fetchone()
            if not row:
                raise PermissionError("Only the assigned HR may configure a never-queued event")
            expected_kind = "feedback" if settings.kind in {"choice", "multiselect"} else settings.kind
            if row["kind"] != expected_kind:
                raise ValueError("Question kind must match its base plan_event (choice/multiselect use feedback)")
            for target in [settings.next_event_id, *(o.next_event_id for o in settings.options)]:
                if target and not conn.execute("SELECT id FROM korsik.plan_events WHERE id=%s AND plan_id=%s AND position>%s",
                    (target, row["plan_id"], row["position"])).fetchone():
                    raise ValueError("A branch must point forward within the same employee plan")
            conn.execute("""INSERT INTO korsik_router.question_configs(event_id,config) VALUES(%s,%s)
                ON CONFLICT(event_id) DO UPDATE SET config=excluded.config""", (event_id, Jsonb(settings.model_dump(mode="json"))))

    def scheduler_tick(self, limit=100):
        from .handlers import Effects
        if not 1 <= limit <= 1000:
            raise ValueError("Scheduler batch size must be 1..1000")
        with self.connection() as conn:
            employees = conn.execute("""SELECT p.id FROM korsik.employees p WHERE p.lifecycle='adapting'
                AND EXISTS(SELECT 1 FROM korsik.plan_events e JOIN korsik.employee_plans plan ON plan.id=e.plan_id
                  LEFT JOIN korsik_router.question_configs c ON c.event_id=e.id
                  WHERE e.employee_id=p.id AND plan.status='active' AND (
                    (e.status IN ('scheduled','queued','awaiting_answer','awaiting_comment') AND e.deadline_at<=clock_timestamp()) OR
                    (e.status IN ('awaiting_answer','awaiting_comment') AND e.next_reminder_at<=clock_timestamp()
                      AND e.reminders_sent<e.max_reminders) OR
                    (e.status='scheduled' AND e.scheduled_at<=clock_timestamp() AND (c.config IS NULL OR NOT EXISTS(
                       SELECT 1 FROM korsik.plan_events waiting JOIN korsik_router.question_configs wc ON wc.event_id=waiting.id
                       WHERE waiting.employee_id=p.id AND waiting.status IN ('queued','awaiting_answer','awaiting_comment')
                         AND wc.config->>'survey_id'=c.config->>'survey_id')))))
                ORDER BY p.id LIMIT %s FOR UPDATE OF p SKIP LOCKED""", (limit,)).fetchall()
            queued = 0
            for employee in employees:
                effects = Effects(conn, employee["id"], "scheduler")
                rows = conn.execute("""SELECT e.*,c.config FROM korsik.plan_events e
                    JOIN korsik.employee_plans p ON p.id=e.plan_id LEFT JOIN korsik_router.question_configs c ON c.event_id=e.id
                    WHERE e.employee_id=%s AND p.status='active' AND e.status IN
                     ('scheduled','queued','awaiting_answer','awaiting_comment') ORDER BY e.position FOR UPDATE OF e""", (employee["id"],)).fetchall()
                blocked = {r["config"]["survey_id"] for r in rows if r["config"] and r["status"] != "scheduled"}
                now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
                for row in rows:
                    if row["deadline_at"] <= now:
                        conn.execute("UPDATE korsik.plan_events SET status=CASE WHEN status='scheduled' THEN 'skipped' ELSE 'overdue' END,next_reminder_at=NULL WHERE id=%s", (row["id"],))
                        conn.execute("""UPDATE korsik.outbox o SET status='cancelled' FROM korsik.messages m
                            WHERE m.id=o.message_id AND m.event_id=%s AND o.status='pending'""", (row["id"],))
                    elif row["status"] == "scheduled" and row["scheduled_at"] <= now:
                        if row["depends_on_id"] and not conn.execute("SELECT id FROM korsik.plan_events WHERE id=%s AND status='completed'", (row["depends_on_id"],)).fetchone():
                            continue
                        if row["config"] and row["config"]["survey_id"] in blocked:
                            continue
                        effects.present(row["id"])
                        if row["config"]:
                            blocked.add(row["config"]["survey_id"])
                        queued += 1
                    elif row["status"] in {"awaiting_answer", "awaiting_comment"} and row["next_reminder_at"] and row["next_reminder_at"] <= now and row["reminders_sent"] < row["max_reminders"]:
                        effects.queue("Напоминаю об опросе: " + row["body"], key=f"reminder:{row['id']}:{row['reminders_sent']+1}",
                                      purpose="reminder", event_id=row["id"])
                        conn.execute("""UPDATE korsik.plan_events SET reminders_sent=reminders_sent+1,
                            next_reminder_at=clock_timestamp()+make_interval(hours=>reminder_hours) WHERE id=%s""", (row["id"],))
                        queued += 1
            return queued
