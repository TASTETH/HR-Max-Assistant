"""Durable channel work, including replies before an employee is activated."""
from __future__ import annotations

from psycopg.types.json import Jsonb

from app.router.schemas import ContextChanged


class TransportRepository:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def queue(conn, key: str, *, chat_id=None, text=None, callback_id=None):
        conn.execute("""INSERT INTO korsik_router.transport_outbox
            (deduplication_key,kind,chat_id,callback_id,body) VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT(deduplication_key) DO NOTHING""",
            (key, "callback" if callback_id else "message", chat_id, callback_id, Jsonb({"text": text} if text else {})))

    def recover(self):
        with self.repository.connection() as conn:
            conn.execute("""UPDATE korsik_router.transport_outbox SET status='uncertain',lease_until=NULL,
                last_error='EXPIRED_SEND_LEASE' WHERE status='sending' AND lease_until<clock_timestamp()""")

    def claim(self):
        with self.repository.connection() as conn:
            return conn.execute("""WITH picked AS (
                SELECT id FROM korsik_router.transport_outbox WHERE status='pending'
                 AND next_attempt_at<=clock_timestamp() ORDER BY (kind='callback') DESC,id
                 LIMIT 1 FOR UPDATE SKIP LOCKED
            ) UPDATE korsik_router.transport_outbox o SET status='sending',attempts=attempts+1,
                generation=generation+1,lease_until=clock_timestamp()+interval '2 minutes'
                FROM picked p WHERE o.id=p.id RETURNING o.*""").fetchone()

    def finish(self, job, external_id=None, *, error=None, uncertain=False, permanent=False):
        with self.repository.connection() as conn:
            conn.execute("""UPDATE korsik_router.transport_outbox SET status=CASE
                 WHEN %s THEN 'uncertain' WHEN %s OR (attempts>=8 AND %s::text IS NOT NULL) THEN 'dead'
                 WHEN %s::text IS NOT NULL THEN 'pending' ELSE 'accepted' END,
                 external_message_id=%s,last_error=%s,lease_until=NULL,
                 next_attempt_at=clock_timestamp()+interval '30 seconds'
                WHERE id=%s AND generation=%s AND status='sending'""",
                (uncertain, permanent, error, error, external_id, error, job["id"], job["generation"]))

    def parts(self, job, bodies: list[dict]):
        with self.repository.connection() as conn:
            self._assert_owner(conn, job)
            for i, body in enumerate(bodies):
                conn.execute("""INSERT INTO korsik_router.delivery_parts(message_id,part,body)
                    VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""", (job["message_id"], i, Jsonb(body)))
            rows = conn.execute("SELECT * FROM korsik_router.delivery_parts WHERE message_id=%s ORDER BY part",
                                (job["message_id"],)).fetchall()
            if [r["body"] for r in rows] != bodies:
                raise ValueError("Message changed after sending was prepared")
            return rows

    @staticmethod
    def _assert_owner(conn, job):
        row = conn.execute("SELECT status,generation FROM korsik.outbox WHERE message_id=%s FOR UPDATE",
                           (job["message_id"],)).fetchone()
        if not row or row["status"] != "sending" or row["generation"] != job["generation"]:
            raise ContextChanged("Send ownership changed")

    def part_sent(self, job, part: int, external_id: str):
        with self.repository.connection() as conn:
            self._assert_owner(conn, job)
            conn.execute("""UPDATE korsik_router.delivery_parts SET external_message_id=%s
                WHERE message_id=%s AND part=%s AND external_message_id IS NULL""",
                (external_id, job["message_id"], part))

    def reserve_slot(self, chat_id: str) -> float:
        """Reserve time in a short transaction; the caller sleeps after commit."""
        with self.repository.connection() as conn:
            keys = ["0:global", "1:chat:" + chat_id]
            for key in keys:
                conn.execute("INSERT INTO korsik_router.rate_slots(key,next_at) VALUES(%s,clock_timestamp()) ON CONFLICT DO NOTHING", (key,))
            rows = conn.execute("SELECT * FROM korsik_router.rate_slots WHERE key=ANY(%s) ORDER BY key FOR UPDATE", (keys,)).fetchall()
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            slot = max(now, *(row["next_at"] for row in rows))
            for key, gap in zip(keys, (0.05, 0.55)):
                conn.execute("UPDATE korsik_router.rate_slots SET next_at=%s+make_interval(secs=>%s) WHERE key=%s", (slot, gap, key))
            return max(0.0, (slot - now).total_seconds())

    def notify_hr(self):
        with self.repository.connection() as conn:
            rows = conn.execute("""SELECT c.id,c.employee_id,c.hr_id,c.reason,c.context_summary,
                e.full_name,d.priority,r.max_chat_id FROM korsik.hr_cases c
                JOIN korsik.employees e ON e.id=c.employee_id JOIN korsik.hr_users h ON h.id=c.hr_id AND h.enabled
                JOIN korsik_router.hr_routes r ON r.hr_id=c.hr_id
                LEFT JOIN korsik_router.case_details d ON d.case_id=c.id
                WHERE c.status='awaiting_hr' AND NOT EXISTS(
                 SELECT 1 FROM korsik_router.transport_outbox t
                 WHERE t.deduplication_key='hr-case:'||c.id||':'||coalesce(d.priority,'low'))
                ORDER BY c.id LIMIT 100""").fetchall()
            for row in rows:
                priority = row["priority"] or "low"
                text = (f"Корсик: обращение №{row['id']} · приоритет {priority}\n"
                        f"Сотрудник: {row['full_name'] or row['employee_id']}\n{row['context_summary'][:2000]}")
                self.queue(conn, f"hr-case:{row['id']}:{priority}", chat_id=row["max_chat_id"], text=text)
            return len(rows)
