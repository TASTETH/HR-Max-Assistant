"""Critical user paths (spec section 15). Real PostgreSQL, console transport, controlled time, fake/absent LLM."""
import re
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from conftest import (buttons_of_last, invite, last_msg_id, make_employee, one, out_texts, press, pump, q, say,
                      start)

SEED = Path(__file__).resolve().parents[2] / "seed"


def next_monday(weeks=1):
    d = date.today()
    return d + timedelta(days=(7 - d.weekday()) % 7 + 7 * (weeks - 1) or 7)


def link(emp_id, user="u1"):
    tok = invite(emp_id)
    start(user, tok)
    return tok


def activate(emp_id):
    from korsik.db import tx
    from korsik.scenario import engine
    with tx() as c:
        c.execute("UPDATE employees SET status='active' WHERE id=%s", (emp_id,))
        e = c.execute("SELECT * FROM employees WHERE id=%s", (emp_id,)).fetchone()
        return engine.materialize_events(c, e)


def seed_corpus():
    from korsik.cli import cmd_seed_corpus
    cmd_seed_corpus(types.SimpleNamespace(folder=str(SEED / "corpus"), org="ООО «Группа ПОЛИПЛАСТИК»"))


# ------------------------------------------------------------------ 1. access, invites, preonboarding
def test_invite_link_preonboarding_and_reuse():
    e = make_employee()
    tok = link(e, "u1")
    emp = one("SELECT * FROM employees WHERE id=%s", (e,))
    assert emp["max_user_id"] == "u1"
    texts = out_texts(e)
    assert any("цифровой помощник HR-службы" in t for t in texts)
    assert texts[-1].startswith("Как мы будем общаться")
    btn = [b for b in buttons_of_last(e) if b["text"] == "На ВЫ"][0]
    press("u1", btn["payload"])
    assert one("SELECT address_form FROM employees WHERE id=%s", (e,))["address_form"] == "vy"
    texts = out_texts(e)
    assert any("Очень приятно с вами познакомиться, Иван" in t for t in texts)
    assert any("Необходимые документы" in t for t in texts)          # GrPP docs list (before start)
    assert "корпоративный транспорт" in texts[-1]                       # GrPP transport ask waits
    # re-opening the same invite continues, no new employee, no repeated preonboarding
    n = len(out_texts(e))
    start("u1", tok)
    assert one("SELECT count(*) n FROM employees")["n"] == 1
    assert "С возвращением" in out_texts(e)[n]
    # another MAX user cannot take the used invite
    start("intruder", tok)
    assert one("SELECT max_user_id FROM employees WHERE id=%s", (e,))["max_user_id"] == "u1"
    # a stale button cannot change anything
    press("u1", btn["payload"])
    assert one("SELECT count(*) n FROM messages WHERE employee_id=%s AND direction='in' AND text='На ВЫ'", (e,))["n"] == 1


def test_permissions_manager_and_private_documents():
    from korsik.api.app import app
    e1 = make_employee(manager_user_id=2)
    e2 = make_employee(full_name="Петров Петр Петрович", manager_user_id=3)
    client = TestClient(app)
    r = client.post("/api/login", json={"login": "boss", "password": "Boss-pass-2026!"})
    csrf = r.json()["csrf"]
    team = client.get("/api/manager/team").json()["team"]
    assert [t["id"] for t in team] == [e1]
    assert "messages" not in team[0] and "documents" not in team[0]
    assert client.get(f"/api/employees/{e1}").status_code == 403
    assert client.get("/api/cases").status_code == 403
    assert client.post(f"/api/employees/{e2}/reply", json={"text": "x"}, headers={"X-CSRF": csrf}).status_code == 403
    # HR uploads a private document -> it never reaches the corporate corpus
    hr = TestClient(app)
    csrf = hr.post("/api/login", json={"login": "hr", "password": "Korsik-HR-2026!"}).json()["csrf"]
    r = hr.post(f"/api/employees/{e1}/documents", headers={"X-CSRF": csrf},
                files={"file": ("offer.txt", "Оффер. ФИО: Иванов Иван Иванович\nДата выхода на работу: 03.11.2026\n"
                                             "на должность Аналитик\nтел. +7 916 123-45-67".encode(), "text/plain")})
    assert r.status_code == 200
    pump()
    sugg = {s["field"]: s for s in q("SELECT * FROM field_suggestions WHERE employee_id=%s", (e1,))}
    assert sugg["start_date"]["value"] == "2026-11-03" and sugg["phone"]["value"] == "+79161234567"
    assert "organization" not in sugg                                  # not invented
    # proposal does not overwrite confirmed data until HR accepts
    assert one("SELECT start_date FROM employees WHERE id=%s", (e1,))["start_date"] != date(2026, 11, 3)
    hr.post(f"/api/suggestions/{sugg['start_date']['id']}/accept", headers={"X-CSRF": csrf})
    assert one("SELECT start_date FROM employees WHERE id=%s", (e1,))["start_date"] == date(2026, 11, 3)
    assert one("SELECT count(*) n FROM corpus_documents")["n"] == 0
    # manager cannot download it
    doc_id = one("SELECT id FROM employee_documents")["id"]
    assert client.get(f"/api/documents/{doc_id}/file").status_code == 403
    # webhook needs the secret
    assert TestClient(app).post("/webhook/max", json={}).status_code == 401


# ------------------------------------------------------------------ 3. scheduler
def test_scheduler_workdays_permission_restart_and_no_duplicates():
    from korsik.scenario.calendar import nth_workday
    mon = next_monday()
    e = make_employee(start_date=mon, tags=["GrPP"], work_format="office")
    link(e)
    activate(e)
    ev = {r["step_key"]: r for r in q("SELECT * FROM events WHERE employee_id=%s", (e,))}
    tz = ZoneInfo("Europe/Moscow")
    assert ev["d3_survey"]["due_at"].astimezone(tz).date() == nth_workday(mon, 3)
    assert ev["d3_survey"]["due_at"].astimezone(tz).strftime("%H:%M") == "17:30"
    assert "d1_1100_map_gtp" not in ev and "d3_gym" not in ev          # GTP-only steps
    assert "d1_1800_end" in ev                                          # office only
    # run everything up to day 2 13:00
    pump(now=datetime.combine(mon, datetime.min.time(), tzinfo=tz).replace(hour=18, minute=30))
    t = datetime.combine(nth_workday(mon, 2), datetime.min.time(), tzinfo=tz).replace(hour=13)
    pump(now=t)
    ev = {r["step_key"]: r for r in q("SELECT * FROM events WHERE employee_id=%s", (e,))}
    assert ev["d2_intro"]["status"] == "scheduled"                     # waits for HR permission (H34)
    assert ev["d1_1000"]["status"] in ("sent", "done")
    sent_before = one("SELECT count(*) n FROM outbox WHERE employee_id=%s", (e,))["n"]
    pump(now=t)                                                          # "restart": nothing new
    assert one("SELECT count(*) n FROM outbox WHERE employee_id=%s", (e,))["n"] == sent_before
    from korsik.db import tx
    with tx() as c:
        c.execute("UPDATE events SET permitted_at=now() WHERE id=%s", (ev["d2_intro"]["id"],))
    pump(now=t)
    assert one("SELECT status FROM events WHERE id=%s", (ev["d2_intro"]["id"],))["status"] == "sent"
    # d3_plan: permission given but plan material missing -> stays pending, visible reason
    with tx() as c:
        c.execute("UPDATE events SET permitted_at=now() WHERE step_key='d3_plan' AND employee_id=%s", (e,))
    pump(now=t + timedelta(days=3))
    plan = one("SELECT * FROM events WHERE step_key='d3_plan' AND employee_id=%s", (e,))
    assert plan["status"] == "scheduled" and "adaptation_plan" in plan["last_error"]


def test_late_link_skips_past_events():
    e = make_employee(start_date=date.today() - timedelta(days=20))
    link(e)
    res = activate(e)
    assert res["skipped_past"] > 0
    texts = out_texts(e)
    assert not any("Я напишу тебе за день до выхода" in t for t in texts)
    assert one("SELECT status FROM events WHERE employee_id=%s AND step_key='d1_1000'", (e,))["status"] == "skipped"


# ------------------------------------------------------------------ 4. surveys
def _test_survey(e, key):
    from korsik.api.app import schedule_test_survey
    schedule_test_survey(e, key, 10, None)
    pump(now=datetime.now(timezone.utc) + timedelta(seconds=30))


def test_survey_comment_clarification_food_and_stale_button(no_llm):
    e = make_employee()
    link(e)
    press("u1", [b for b in buttons_of_last(e) if b["text"] == "На ТЫ"][0]["payload"])
    _test_survey(e, "check2w")
    q1_buttons = buttons_of_last(e)
    press("u1", [b for b in q1_buttons if b["text"] == "Нет"][0]["payload"])
    assert "причину" in out_texts(e)[-1]
    say("u1", "Задачи пока не объяснили")
    a = one("SELECT * FROM survey_answers WHERE question_id='q1'")
    assert a["option_ids"] == ["no"] and a["comment"] == "Задачи пока не объяснили"
    say("u1", "да")       # two open questions (transport offer + survey q2) -> asks which one
    assert "это ответ на какой вопрос" in out_texts(e)[-1]
    press("u1", [b for b in buttons_of_last(e) if "достаточно информации" in b["text"]][0]["payload"])
    assert one("SELECT option_ids FROM survey_answers WHERE question_id='q2'")["option_ids"] == ["yes"]
    say("u1", "7")                                                      # scale q3 < 8 -> comment
    assert one("SELECT score FROM survey_answers WHERE question_id='q3'")["score"] == 7
    say("u1", "Где столовая?")                                          # not taken as the reason
    assert "ответ на мой вопрос или отдельный вопрос" in out_texts(e)[-1]
    assert one("SELECT comment FROM survey_answers WHERE question_id='q3'")["comment"] is None
    mid = one("SELECT max(id) m FROM messages WHERE employee_id=%s AND text='Где столовая?'", (e,))["m"]
    press("u1", f"cl:{mid}:question")
    say("u1", "/Food")                                                  # global command, survey keeps waiting
    assert one("SELECT current_q FROM survey_runs WHERE status='active'")["current_q"] == "q3"
    say("u1", "Руководитель редко отвечает")
    assert one("SELECT comment FROM survey_answers WHERE question_id='q3'")["comment"] == "Руководитель редко отвечает"
    assert one("SELECT current_q FROM survey_runs WHERE status='active'")["current_q"] == "q4"
    # old q1 button is stale and changes nothing
    press("u1", [b for b in q1_buttons if b["text"] == "Да"][0]["payload"])
    assert one("SELECT option_ids FROM survey_answers WHERE question_id='q1'")["option_ids"] == ["no"]


def test_multi_select_exclusive_and_own_option(no_llm):
    e = make_employee()
    link(e)
    _test_survey(e, "two_months_grpp")
    press("u1", [b for b in buttons_of_last(e) if b["text"].startswith("С отличным")][0]["payload"])
    btn = {b["text"]: b["payload"] for b in buttons_of_last(e)}
    press("u1", btn["Руководитель"])
    press("u1", btn["Никто!"])
    run = one("SELECT draft FROM survey_runs WHERE status='active'")
    assert run["draft"]["q2"] == ["nobody"]
    press("u1", btn["Коллеги"])
    assert one("SELECT draft FROM survey_runs WHERE status='active'")["draft"]["q2"] == ["colleagues"]
    assert one("SELECT count(*) n FROM survey_answers WHERE question_id='q2'")["n"] == 0   # draft only
    press("u1", btn["Готово ➜"])
    assert one("SELECT option_labels FROM survey_answers WHERE question_id='q2'")["option_labels"] == ["Коллеги"]
    press("u1", [b for b in buttons_of_last(e) if b["text"] == "Свой вариант"][0]["payload"])
    say("u1", "Наставник на первые недели")
    a = one("SELECT * FROM survey_answers WHERE question_id='q3'")
    assert a["option_ids"] == ["own"] and a["text"] == "Наставник на первые недели"
    assert one("SELECT status FROM survey_runs WHERE survey_key LIKE 'two_months_grpp%%'")["status"] == "completed"


def test_reminders_max_two_and_none_after_completion():
    from korsik.db import tx
    from korsik.scenario.survey import send_due_reminders
    e = make_employee()
    link(e)
    _test_survey(e, "day3")
    run = one("SELECT * FROM survey_runs WHERE status='active'")
    assert run["started_at"] is not None and run["next_reminder_at"] is not None
    for _ in range(4):
        with tx() as c:
            c.execute("UPDATE survey_runs SET next_reminder_at=now()-interval '1 minute' WHERE id=%s AND next_reminder_at IS NOT NULL", (run["id"],))
            send_due_reminders(c)
    assert one("SELECT reminders_sent FROM survey_runs WHERE id=%s", (run["id"],))["reminders_sent"] == 2
    assert len([t for t in out_texts(e) if "не прошел опрос" in t]) == 2


# ------------------------------------------------------------------ 5. seamless HR conversation
def test_hr_takeover_same_chat_and_other_employee_keeps_automation(no_llm):
    from korsik.db import tx
    from korsik import cases
    e = make_employee()
    e2 = make_employee(full_name="Сидорова Анна Петровна", short_name="Анна")
    link(e, "u1")
    link(e2, "u2")
    _test_survey(e, "check2w")
    say("u1", "Какой пароль от Wi-Fi в офисе?")
    case = one("SELECT * FROM cases WHERE employee_id=%s", (e,))
    assert case["reason"] == "no_answer" and case["status"] == "new"       # no corpus: accepted for HR
    assert out_texts(e)[-1] == "Уточню этот вопрос и вернусь с ответом."
    assert one("SELECT count(*) n FROM notifications WHERE case_id=%s", (case["id"],))["n"] == 1
    assert one("SELECT dialog_mode FROM employees WHERE id=%s", (e,))["dialog_mode"] == "waiting_hr"
    # a late automatic answer prepared before takeover
    with tx() as c:
        emp = c.execute("SELECT * FROM employees WHERE id=%s", (e,)).fetchone()
        from korsik.outbound import queue_to_employee
        queue_to_employee(c, emp, "поздний автоответ", author_kind="bot_llm", source="llm")
        hr = c.execute("SELECT * FROM users WHERE login='hr'").fetchone()
        cases.take(c, hr, case["id"])
    pump()
    assert one("SELECT status FROM outbox WHERE body->>'text'='поздний автоответ'")["status"] == "cancelled"
    with tx() as c:
        cases.hr_reply(c, hr, e, "Пароль выдаёт IT, уточнила — напишу.")
    pump()
    assert one("SELECT status FROM outbox WHERE body->>'text' LIKE 'Пароль выдаёт%%'")["status"] == "sent"
    n = len(out_texts(e))
    say("u1", "Спасибо, жду")                                          # goes to HR, no bot reply
    assert len(out_texts(e)) == n
    assert one("SELECT case_id FROM messages WHERE text='Спасибо, жду'")["case_id"] == case["id"]
    # scenario events and reminders for this employee are paused; another employee is served
    from korsik.api.app import schedule_test_survey
    schedule_test_survey(e2, "day3", 10, None)
    with tx() as c:
        c.execute("UPDATE survey_runs SET next_reminder_at=now()-interval '1 minute' WHERE employee_id=%s", (e,))
    pump(now=datetime.now(timezone.utc) + timedelta(seconds=30))
    assert len(out_texts(e)) == n
    assert any("Вопрос 1 из 3" in t for t in out_texts(e2))
    # late RAG job result after takeover never answers over HR
    from korsik.freeflow import handle_free_message
    mid = one("SELECT id FROM messages WHERE text='Спасибо, жду'")["id"]
    handle_free_message({"employee_id": e, "message_id": mid, "revision": 0}, "late")
    pump()
    assert len(out_texts(e)) == n
    # explicit release -> survey resumes once, overdue reminder moved (no burst)
    with tx() as c:
        mode = cases.release(c, hr, e)
    pump()
    assert mode == "auto"
    assert one("SELECT status FROM cases WHERE id=%s", (case["id"],))["status"] == "resolved"
    after = out_texts(e)[n:]
    assert len([t for t in after if "Вопрос 1 из 9" in t]) == 1 and not any("не прошел опрос" in t for t in after)


def test_uncertain_delivery_does_not_block_case(no_llm, monkeypatch):
    from korsik import transport
    e = make_employee()
    link(e)

    class Flaky(transport.ConsoleTransport):
        def send(self, chat_id, body):
            raise transport.Uncertain("ReadTimeout")
    transport.set_transport(Flaky())
    say("u1", "Хочу поговорить с человеком из HR")
    assert one("SELECT reason FROM cases WHERE employee_id=%s", (e,))["reason"] == "human_request"
    assert one("SELECT count(*) n FROM outbox WHERE status='uncertain'")["n"] >= 1
    assert one("SELECT count(*) n FROM notifications")["n"] == 1
    assert one("SELECT status FROM outbox WHERE body->>'text' LIKE 'Хорошо, попрошу%%'")["status"] == "uncertain"


# ------------------------------------------------------------------ 6. analysis-first policy
def test_mixed_problem_not_answered_by_faq(monkeypatch):
    from korsik import analysis
    from korsik.rag import answer
    monkeypatch.setattr(analysis, "chat_json", lambda *a, **k: {
        "sentiment": "negative", "topics": ["access"], "signals": [{"type": "access", "quote": "не выдали доступы"}],
        "summary": "Нет доступов, не может работать", "priority": "medium", "requires_hr": True})
    called = []
    monkeypatch.setattr(answer, "answer_question", lambda *a, **k: called.append(1))
    e = make_employee()
    link(e)
    say("u1", "Мне не выдали доступы, работать не могу. Что делать?")
    case = one("SELECT * FROM cases WHERE employee_id=%s", (e,))
    assert case["reason"] == "feedback_signal" and case["priority"] == "medium" and not called


def test_leave_signal_without_llm_and_negation(no_llm):
    from korsik.analysis import rule_signals
    assert not [s for s in rule_signals("Не хочу увольняться, всё нравится") if s["type"] == "leave"]
    assert [s for s in rule_signals("Думаю уволиться, тяжело") if s["type"] == "leave"]
    e = make_employee()
    link(e)
    _test_survey(e, "final")
    press("u1", [b for b in buttons_of_last(e) if b["text"] == "Со всем справился"][0]["payload"])
    press("u1", [b for b in buttons_of_last(e) if b["text"].startswith("Нет, задумываюсь")][0]["payload"])
    case = one("SELECT * FROM cases WHERE employee_id=%s", (e,))
    assert case["priority"] == "high" and case["reason"] == "feedback_signal"
    an = one("SELECT * FROM analyses WHERE employee_id=%s", (e,))
    assert an["sentiment"] == "unknown"                                  # not invented without LLM
    press("u1", [b for b in buttons_of_last(e) if b["text"] == "Обсудить лично"][0]["payload"])
    assert one("SELECT count(*) n FROM cases WHERE reason='human_request'")["n"] == 1


# ------------------------------------------------------------------ 7. RAG evidence checks
def _fake_rag(quote_from_context: bool, extra_number=False):
    def fake(system, user, schema, **k):
        frag = re.search(r"\[(F\d+)\] «Антикоррупционная политика», стр\./слайд 9:\n(.+?)(?:\n\n\[F|\Z)", user, re.S)
        fid = frag.group(1) if frag else "F1"
        quote = ("Не допускаются подарки от имени Общества, его работников и представителей третьим лицам в виде "
                 "денежных средств") if quote_from_context else "подарки до 3000 рублей разрешены всегда"
        ans = "Нет, денежные подарки запрещены." + (" Лимит 5000 рублей." if extra_number else "")
        return {"status": "answered", "answer": ans, "claims": [{"text": ans, "fragment": fid, "quote": quote}]}
    return fake


def test_rag_requires_literal_evidence(monkeypatch):
    seed_corpus()
    from korsik.rag import answer
    e = make_employee()
    emp = one("SELECT * FROM employees WHERE id=%s", (e,))
    monkeypatch.setattr(answer, "chat_json", _fake_rag(True))
    r = answer.answer_question("Можно ли подарить контрагенту наличные деньги?", emp)
    assert r.status == "answered" and r.sources[0]["page"] == 9 and "слайд 9" in answer.format_answer(r)
    monkeypatch.setattr(answer, "chat_json", _fake_rag(False))
    assert answer.answer_question("Можно ли подарить контрагенту наличные деньги?", emp).status == "insufficient_evidence"
    monkeypatch.setattr(answer, "chat_json", _fake_rag(True, extra_number=True))
    assert answer.answer_question("Можно ли подарить контрагенту наличные деньги?", emp).status == "insufficient_evidence"
    # applicability: another organization does not get the group's internal rules
    other = dict(emp, organization="ООО «Другая компания»")
    monkeypatch.setattr(answer, "chat_json", _fake_rag(True))
    assert answer.answer_question("Можно ли подарить контрагенту наличные деньги?", other).status == "insufficient_evidence"


def test_service_error_differs_from_no_answer(no_llm):
    seed_corpus()
    from korsik.rag import answer
    emp = one("SELECT * FROM employees WHERE id=%s", (make_employee(),))
    assert answer.answer_question("Можно ли дарить деньги?", emp).status == "service_error"


# ------------------------------------------------------------------ 8. corpus versions from the cabinet path
def test_corpus_publish_withdraw_and_failed_build_keeps_active(tmp_path, monkeypatch):
    from korsik import corpus
    from korsik.config import get_settings
    from korsik.db import tx
    from korsik.rag.index import search
    seed_corpus()
    v1 = one("SELECT id FROM corpus_versions WHERE status='active'")["id"]
    s = get_settings()
    (s.corpus_dir / "wifi.txt").write_text("Пароль гостевой сети Wi-Fi выдаёт служба ИТ по заявке на портале.", encoding="utf-8")
    with tx() as c:
        d = c.execute("""INSERT INTO corpus_documents(title, filename, stored_name, sha256, organizations)
                         VALUES ('Памятка Wi-Fi','wifi.txt','wifi.txt','x',ARRAY['*']) RETURNING id""").fetchone()["id"]
    corpus.process_extract(d)
    assert not [h for h in search("пароль wi-fi", None)["hits"] if h["document_id"] == d]  # not published yet
    # failing build (embedding model unreachable) keeps the working corpus
    monkeypatch.setenv("EMBED_MODEL", "bge-m3")
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")
    get_settings.cache_clear()
    with pytest.raises(Exception):
        corpus.run_build({"add": [d], "remove": []})
    assert one("SELECT id FROM corpus_versions WHERE status='active'")["id"] == v1
    assert one("SELECT status FROM corpus_documents WHERE id=%s", (d,))["status"] == "error"
    monkeypatch.setenv("EMBED_MODEL", "")
    get_settings.cache_clear()
    corpus.run_build({"add": [d], "remove": []})
    assert [h for h in search("пароль wi-fi", None)["hits"] if h["document_id"] == d]
    with tx() as c:
        corpus.request_withdraw(c, d, 1)
    assert not [h for h in search("пароль wi-fi", None)["hits"] if h["document_id"] == d]  # immediate


# ------------------------------------------------------------------ 9. analytics, manager notice, digest
def test_analytics_digest_and_manager_notification(no_llm):
    from korsik import analytics
    from korsik.analysis import run_analysis_job
    from korsik.db import tx
    e = make_employee(manager_user_id=2)
    link(e)
    _test_survey(e, "day3")
    press("u1", [b for b in buttons_of_last(e) if b["text"] == "Все отлично!"][0]["payload"])
    press("u1", [b for b in buttons_of_last(e) if b["text"] == "Никак, все заняты"][0]["payload"])
    say("u1", "Со мной никто не общается, наставник игнорирует")
    case = one("SELECT * FROM cases WHERE employee_id=%s", (e,))
    assert case["topic"] == "colleagues" and case["priority"] == "medium"
    note = one("SELECT * FROM notifications WHERE recipient_user_id=2")
    assert note and "встреча" in note["title"].lower() and "игнорирует" not in note["body"]   # basis, no private text
    assert note["channel_status"] == "no_channel"                       # manager MAX not linked: no fake success
    # repeated analysis does not double count
    sa = one("SELECT id FROM survey_answers WHERE question_id='q2'")["id"]
    run_analysis_job({"source_type": "survey_answer", "source_id": sa}, "r1")
    run_analysis_job({"source_type": "survey_answer", "source_id": sa}, "r2")
    with tx() as c:
        d = analytics.compute(c, datetime.now(timezone.utc) - timedelta(days=1), datetime.now(timezone.utc) + timedelta(minutes=1))
    assert d["sentiment"]["unknown"]["items"] == 1 and "negative" not in d["sentiment"]
    assert d["survey_answers"]["employees"] == 1
    # weekly digest: once per period, explicit "no comparison" on first week
    with tx() as c:
        hr = c.execute("SELECT * FROM users WHERE login='hr'").fetchone()
        nxt = datetime.now(timezone.utc) + timedelta(days=7)
        assert analytics.weekly(c, hr, now=nxt) is not None
        assert analytics.weekly(c, hr, now=nxt) is None
    dg = one("SELECT * FROM digests")
    assert "первый дайджест" in dg["text"]


# ------------------------------------------------------------------ regressions found by independent review
def test_reminder_keeps_pending_comment_and_cancelled_ask_returns(no_llm):
    from korsik import cases
    from korsik.db import tx
    from korsik.scenario import engine
    from korsik.scenario.survey import send_due_reminders
    e = make_employee(manager_user_id=2)
    link(e)
    _test_survey(e, "check2w")
    press("u1", [b for b in buttons_of_last(e) if b["text"] == "Да"][0]["payload"])
    press("u1", [b for b in buttons_of_last(e) if b["text"] == "Да"][0]["payload"])
    press("u1", [b for b in buttons_of_last(e) if b["text"] == "5"][0]["payload"])      # q3 -> comment prompt
    with tx() as c:
        c.execute("UPDATE survey_runs SET next_reminder_at=now()-interval '1 minute' WHERE status='active'")
        send_due_reminders(c)
    pump()
    say("u1", "Не хватает обратной связи от руководителя")
    assert one("SELECT comment FROM survey_answers WHERE question_id='q3'")["comment"] == "Не хватает обратной связи от руководителя"
    # an ask cancelled by HR takeover comes back after release
    say("u1", "Хочу поговорить с человеком из HR")
    case = one("SELECT * FROM cases WHERE reason='human_request'")
    with tx() as c:
        c.execute("UPDATE employees SET status='active' WHERE id=%s", (e,))
        emp = c.execute("SELECT * FROM employees WHERE id=%s", (e,)).fetchone()
        engine.materialize_events(c, emp)
        ev = c.execute("SELECT * FROM events WHERE employee_id=%s AND step_key='d1_1230_food'", (e,)).fetchone()
        engine.execute_event(c, emp, ev)
        hr = c.execute("SELECT * FROM users WHERE login='hr'").fetchone()
        cases.take(c, hr, case["id"])
    pump()
    with tx() as c:
        cases.release(c, hr, e)
    pump()
    assert one("SELECT count(*) n FROM outbox WHERE employee_id=%s AND status='sent' AND body->>'text' LIKE 'Хочешь, подскажу%%'", (e,))["n"] == 1
    # a complaint about the manager is never forwarded to that manager
    with tx() as c:
        emp = c.execute("SELECT * FROM employees WHERE id=%s", (e,)).fetchone()
        cases.open_case(c, emp, reason="feedback_signal", priority="medium", topic="manager", question="x", dedupe_key="mgr")
    assert one("SELECT count(*) n FROM notifications WHERE recipient_user_id=2")["n"] == 0
