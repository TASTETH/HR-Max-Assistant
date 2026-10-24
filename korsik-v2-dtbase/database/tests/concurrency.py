"""Native PostgreSQL 17+, isolated seeded test DB ONLY.
Needs two independent connections. Never substitute SQLite/PGlite for these tests.
Fixtures intentionally remain in the disposable database for inspection.
"""
import hashlib
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import psycopg


def main():
    if os.getenv("KORSIK_TEST_ALLOW_WRITE") != "1":
        raise SystemExit("Set KORSIK_TEST_ALLOW_WRITE=1 and KORSIK_TEST_DSN to a disposable seeded DB")
    dsn = os.environ["KORSIK_TEST_DSN"]
    suffix = secrets.token_hex(8)
    digest = hashlib.sha256(suffix.encode()).digest()
    with psycopg.connect(dsn, autocommit=True) as c:
        assert int(c.execute("show server_version_num").fetchone()[0]) >= 170000
        h, v = c.execute("SELECT h.id,v.id FROM korsik.hr_users h CROSS JOIN korsik.plan_versions v "
                         "WHERE h.auth_subject='demo-hr-subject' AND v.published_at IS NOT NULL ORDER BY v.id LIMIT 1").fetchone()
        eid = c.execute("INSERT INTO korsik.employees(hr_id,external_employee_id) VALUES(%s,%s) RETURNING id",
                        (h,'RACE-'+suffix)).fetchone()[0]
        c.execute("INSERT INTO korsik.invitations(employee_id,token_hash,expires_at) VALUES(%s,%s,now()+interval '1 day')",(eid,digest))
    winner_holds_lock = Event()
    loser_started = Event()

    def activate_winner():
        with psycopg.connect(dsn) as c:
            r = c.execute("SELECT korsik.activate_invitation(%s,%s,%s)",
                          (digest,'race-account-A-'+suffix,'race-chat-A-'+suffix)).fetchone()[0]
            winner_holds_lock.set()
            assert loser_started.wait(15), "Second activator never started"
            return r

    def activate_loser():
        assert winner_holds_lock.wait(15)
        try:
            with psycopg.connect(dsn) as c:
                c.execute("SET LOCAL statement_timeout='15s'")
                loser_started.set()
                c.execute("SELECT korsik.activate_invitation(%s,%s,%s)",
                          (digest,'race-account-B-'+suffix,'race-chat-B-'+suffix))
        except psycopg.errors.RaiseException:
            return "rejected"
        raise AssertionError("Both concurrent activations succeeded")

    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(activate_winner);b=pool.submit(activate_loser)
        assert a.result(timeout=30)==eid
        assert b.result(timeout=30)=="rejected"
    print("PASS concurrent invitation activation: exactly one business result")

    with psycopg.connect(dsn) as c:
        unit=c.execute("SELECT id FROM korsik.org_units WHERE code='DEMO'").fetchone()[0]
        c.execute("SELECT korsik.save_registration(%s,0,'full_name','Тест Конкуренции')",(eid,))
        c.execute("SELECT korsik.save_registration(%s,1,'phone','+79990009999')",(eid,))
        c.execute("SELECT korsik.save_registration(%s,2,'confirm','yes')",(eid,))
        c.execute("SELECT korsik.verify_registration(%s,%s,%s,'Инженер',CURRENT_DATE,'Europe/Moscow')",(h,eid,unit))
        pid=c.execute("SELECT korsik.assign_plan(%s,%s,%s)",(h,eid,v)).fetchone()[0]
        c.execute("UPDATE korsik.plan_events SET scheduled_at=now()-interval '1 minute',deadline_at=now()+interval '1 day' WHERE plan_id=%s AND code='welcome'",(pid,))
        ev=c.execute("SELECT id FROM korsik.plan_events WHERE plan_id=%s AND code='welcome'",(pid,)).fetchone()[0]
    # Hold worker A's transaction open deliberately; B must skip its locked employee.
    with psycopg.connect(dsn) as a, psycopg.connect(dsn) as b:
        a.execute("SELECT korsik.prepare_due_events(1000)")
        b.execute("SET LOCAL statement_timeout='5s'")
        b.execute("SELECT korsik.prepare_due_events(1000)")
        b.commit()
        a.commit()
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT count(*) FROM korsik.messages WHERE event_id=%s AND deduplication_key=%s",(ev,f'event:{ev}')).fetchone()[0]==1
    print("PASS two scheduler transactions: one event message")

    with psycopg.connect(dsn) as a, psycopg.connect(dsn) as b:
        a_rows=a.execute("SELECT * FROM korsik.claim_outbox(1000)").fetchall()
        b.execute("SET LOCAL statement_timeout='5s'")
        b_rows=b.execute("SELECT * FROM korsik.claim_outbox(1000)").fetchall()
        assert a_rows, "No due message to test"
        assert not ({r[0] for r in a_rows}&{r[0] for r in b_rows}), "Workers claimed the same message"
        # Roll back leases; no external calls and no persistent sending state.
        a.rollback();b.rollback()
    print("PASS two outbox workers: disjoint claims")

    # COMMIT is the essential distinction from the original disjoint-message test.
    with psycopg.connect(dsn) as c:
        c.execute("SELECT korsik.queue_message(%s,'bot','serial A',%s,'notice')",(eid,'serial-a-'+suffix))
        c.execute("SELECT korsik.queue_message(%s,'bot','serial B',%s,'notice')",(eid,'serial-b-'+suffix))
    with psycopg.connect(dsn) as a, psycopg.connect(dsn) as b:
        first=a.execute("SELECT * FROM korsik.claim_outbox(1000)").fetchall()
        assert len({r[5] for r in first})==len(first), "More than one claim per employee"
        a.commit()
        second=b.execute("SELECT * FROM korsik.claim_outbox(1000)").fetchall()
        assert not ({r[5] for r in first}&{r[5] for r in second}), "Same employee after COMMIT"
        b.rollback()
        for r in first:
            a.execute("SELECT korsik.finish_send(%s,%s,NULL,'TEST_NO_NETWORK',true)",(r[0],r[1]))
        a.commit()
        third=b.execute("SELECT * FROM korsik.claim_outbox(1000)").fetchall()
        assert not ({r[5] for r in first}&{r[5] for r in third}), "Uncertain did not block employee"
        b.rollback()
        # No real network call was made; safe fixture cleanup.
        for r in first:
            a.execute("UPDATE korsik.outbox SET status='pending',generation=generation+1,lease_until=NULL WHERE message_id=%s",(r[0],))
        a.commit()
    print("PASS delivery serialization after COMMIT and uncertain")

    event='race-webhook-'+suffix
    first_done=Event();second_started=Event()
    def inbox_a():
        with psycopg.connect(dsn) as c:
            c.execute("INSERT INTO korsik.max_inbox(event_id,payload) VALUES(%s,'{}')",(event,))
            first_done.set();assert second_started.wait(15)
    def inbox_b():
        assert first_done.wait(15)
        with psycopg.connect(dsn) as c:
            c.execute("SET LOCAL statement_timeout='15s'");second_started.set()
            return c.execute("INSERT INTO korsik.max_inbox(event_id,payload,received_at) "
              "VALUES(%s,'{}',now()+interval '1 month') ON CONFLICT DO NOTHING RETURNING event_id",(event,)).fetchone()
    with ThreadPoolExecutor(max_workers=2) as pool:
        x=pool.submit(inbox_a);y=pool.submit(inbox_b)
        x.result(timeout=30);assert y.result(timeout=30) is None
    print("PASS concurrent webhook duplicate across months")


if __name__=="__main__":
    main()
