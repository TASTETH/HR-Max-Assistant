"""Background handling of a free employee message: problem analysis first, then RAG.
No DB transaction is held during LLM calls; before replying the dialog revision is
re-checked (and the sender checks it once more right before the network call)."""
from __future__ import annotations

import logging

from .analysis import analyze_text, is_personal_data_question, rule_signals, VERSION
from .cases import open_case
from .db import J, tx
from .dialog import is_question_like, say
from .outbound import queue_to_employee
from .rag.answer import answer_question, format_answer

log = logging.getLogger(__name__)
CLARIFY = ("Уточню этот вопрос и вернусь с ответом.", "Уточню этот вопрос и вернусь с ответом.")
CLARIFY_AGAIN = ("Этот вопрос тоже уточню и вернусь с ответом.", "Этот вопрос тоже уточню и вернусь с ответом.")


def _load(emp_id, mid):
    with tx() as c:
        emp = c.execute("SELECT * FROM employees WHERE id=%s", (emp_id,)).fetchone()
        msg = c.execute("SELECT * FROM messages WHERE id=%s", (mid,)).fetchone()
        hist = c.execute("""SELECT author_kind, text FROM messages WHERE employee_id=%s AND id < %s
                            AND author_kind IN ('employee','bot_llm','hr') ORDER BY id DESC LIMIT 4""",
                         (emp_id, mid)).fetchall()
    return emp, msg, [f"{'Сотрудник' if h['author_kind'] == 'employee' else 'Корсик'}: {h['text'][:200]}"
                      for h in reversed(hist)]


def _current(c, emp_id, revision) -> dict | None:
    emp = c.execute("SELECT * FROM employees WHERE id=%s FOR UPDATE", (emp_id,)).fetchone()
    if emp["dialog_mode"] == "hr_active" or emp["dialog_revision"] != revision:
        return None
    return emp


def _register(emp_id, revision, mid, *, reason, priority, topic, question, needs_review=False, ctx=None, rid=""):
    with tx() as c:
        emp = c.execute("SELECT * FROM employees WHERE id=%s FOR UPDATE", (emp_id,)).fetchone()
        had_open = c.execute("SELECT count(*) n FROM cases WHERE employee_id=%s AND status<>'resolved'",
                             (emp_id,)).fetchone()["n"]
        case = open_case(c, emp, reason=reason, priority=priority, topic=topic, question=question,
                         root_message_id=mid, dedupe_key=f"msg:{mid}", needs_review=needs_review, context=ctx)
        cur = _current(c, emp_id, revision)
        if cur:  # HR has not taken the dialog meanwhile -> one calm message
            t = CLARIFY_AGAIN if had_open else CLARIFY
            queue_to_employee(c, cur, t[1] if cur.get("address_form") == "vy" else t[0],
                              author_kind="bot_system", source="system", case_id=case["id"], request_id=rid,
                              payload={"reply_to": mid})


def _already_handled(mid: int) -> bool:
    """Idempotency after a crash between committing the reply and marking the job done."""
    with tx() as c:
        return bool(c.execute("""SELECT 1 FROM messages WHERE direction='out' AND payload->>'reply_to' = %s
                                 UNION ALL SELECT 1 FROM cases WHERE dedupe_key = %s LIMIT 1""",
                              (str(mid), f"msg:{mid}")).fetchone())


def register_failure(payload: dict, rid: str, error: str) -> None:
    if _already_handled(payload["message_id"]):
        return
    with tx() as c:
        msg = c.execute("SELECT text FROM messages WHERE id=%s", (payload["message_id"],)).fetchone()
    _register(payload["employee_id"], payload["revision"], payload["message_id"], reason="service_error", priority="low",
              topic="other", question=msg["text"], needs_review=True, ctx={"error": error}, rid=rid)


def handle_free_message(payload: dict, rid: str) -> None:
    emp_id, mid, revision = payload["employee_id"], payload["message_id"], payload["revision"]
    if not payload.get("retry_of") and _already_handled(mid):
        return
    emp, msg, history = _load(emp_id, mid)
    if emp["dialog_mode"] == "hr_active":
        return
    text = msg["text"]
    signals = rule_signals(text)
    # 1. problem signals: analysis + human policy BEFORE any FAQ answer
    if signals or (not is_question_like(text) and len(text) > 25):
        result, status, err = analyze_text(text, None, rid)
        with tx() as c:
            c.execute("""INSERT INTO analyses(employee_id, source_type, source_id, analyzer_version, status, sentiment,
                         topics, signals, summary, priority, requires_hr, model)
                         VALUES (%s,'message',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                      (emp_id, mid, VERSION, status, result["sentiment"], result["topics"], J(result["signals"]),
                       result["summary"], result["priority"], result["requires_hr"], None if status != "ok" else "llm"))
        if result["requires_hr"] and result["priority"] in ("medium", "high"):
            _register(emp_id, revision, mid, reason="feedback_signal", priority=result["priority"],
                      topic=result["topics"][0], question=text, needs_review=status != "ok",
                      ctx={"summary": result["summary"], "signals": result["signals"]}, rid=rid)
            return
        if not is_question_like(text):
            with tx() as c:
                cur = _current(c, emp_id, revision)
                if cur:
                    say(c, cur, "Спасибо, что поделился! Если появятся вопросы о работе в компании — пиши, я помогу.",
                        "Спасибо, что поделились! Если появятся вопросы о работе в компании — пишите, я помогу.",
                        payload={"reply_to": mid})
            return
    # 2. personal HR data -> responsible person
    if is_personal_data_question(text):
        _register(emp_id, revision, mid, reason="personal_data", priority="low", topic="hr_admin",
                  question=text, rid=rid)
        return
    # 3. reference question -> RAG
    r = answer_question(text, emp, history=history, request_id=rid)
    log.info("rag request_id=%s status=%s debug=%s", rid, r.status, {k: v for k, v in r.debug.items() if k != "fragments"})
    if r.status == "answered":
        with tx() as c:
            cur = _current(c, emp_id, revision)
            if not cur:
                return
            queue_to_employee(c, cur, format_answer(r), author_kind="bot_llm", source="llm", request_id=rid,
                              corpus_version_id=r.corpus_version_id,
                              cited_doc_ids=sorted({s_["document_id"] for s_ in r.sources}),
                              payload={"reply_to": mid, "sources": r.sources, "rag_status": r.status})
        return
    if r.status == "needs_clarification":
        with tx() as c:
            cur = _current(c, emp_id, revision)
            if cur:
                queue_to_employee(c, cur, r.text, author_kind="bot_llm", source="llm", request_id=rid,
                                  payload={"reply_to": mid, "rag_status": r.status})
        return
    if r.status == "insufficient_evidence":
        _register(emp_id, revision, mid, reason="no_answer", priority="low", topic="other", question=text,
                  ctx={"rag": r.debug.get("reason") or r.debug.get("problems")}, rid=rid)
        return
    # service_error: technical problem in logs; the question is still accepted for manual handling
    _register(emp_id, revision, mid, reason="service_error", priority="low", topic="other", question=text,
              needs_review=True, ctx={"error": r.debug.get("error")}, rid=rid)
