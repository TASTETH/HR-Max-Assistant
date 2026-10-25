"""Typed surveys: single, yes/no, scale 1–10, multi (draft + confirm), own option, free text,
conditional comment. One active survey per employee; others wait in a queue.

Button payloads carry run id + nonce; a stale button (old question) never changes another
question. Reminders (row 70): at most two — next day and the day after at REMINDER_TIME,
counted from the moment MAX accepted the survey's opening message.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import get_settings
from ..db import J, enqueue
from ..outbound import personalize, queue_to_employee
from .calendar import next_local
from .engine import scenario

log = logging.getLogger(__name__)


def survey_def(key: str) -> dict:
    return scenario()["surveys"][key.split("#", 1)[0]]


def _q(defn: dict, qid: str) -> dict:
    return next(q for q in defn["questions"] if q["id"] == qid)


def _say(conn, emp, text, text_vy=None, **kw) -> int:
    return queue_to_employee(conn, emp, personalize(emp, text, text_vy), author_kind="bot_script",
                             source="scenario", **kw)


def start_survey(conn, emp: dict, key: str, event_id: int | None, request_id=None) -> dict:
    run = conn.execute(
        """INSERT INTO survey_runs(employee_id, event_id, survey_key, status) VALUES (%s,%s,%s,'queued')
           ON CONFLICT (employee_id, survey_key) DO NOTHING RETURNING *""",
        (emp["id"], event_id, key)).fetchone()
    if not run:
        return conn.execute("SELECT * FROM survey_runs WHERE employee_id=%s AND survey_key=%s",
                            (emp["id"], key)).fetchone()
    active = conn.execute("SELECT id FROM survey_runs WHERE employee_id=%s AND status='active'",
                          (emp["id"],)).fetchone()
    if active:
        return run  # stays queued; started when the active one completes
    return _activate(conn, emp, run)


def _activate(conn, emp: dict, run: dict) -> dict:
    defn = survey_def(run["survey_key"])
    conn.execute("UPDATE survey_runs SET status='active' WHERE id=%s", (run["id"],))
    run["status"] = "active"
    opening = None
    if defn.get("intro"):
        opening = _say(conn, emp, defn["intro"]["text"], defn["intro"].get("text_vy"), survey_run_id=run["id"])
    first = defn["questions"][0]
    ob = send_question(conn, emp, run, first, opening=opening is None)
    return run


def _buttons(emp, run, q, nonce, draft=None) -> list:
    rid = run["id"]
    if q["type"] in ("single", "yes_no"):
        return [[{"text": personalize(emp, o["label"], o.get("label_vy")), "payload": f"s:{rid}:{nonce}:{o['id']}"}]
                for o in q["options"]]
    if q["type"] == "scale":
        return [[{"text": str(i), "payload": f"s:{rid}:{nonce}:{i}"} for i in range(1, 6)],
                [{"text": str(i), "payload": f"s:{rid}:{nonce}:{i}"} for i in range(6, 11)]]
    if q["type"] == "multi":
        sel = set(draft or [])
        rows = [[{"text": ("✓ " if o["id"] in sel else "") + o["label"], "payload": f"m:{rid}:{nonce}:{o['id']}"}]
                for o in q["options"]]
        rows.append([{"text": "Готово ➜", "payload": f"md:{rid}:{nonce}"}])
        return rows
    return []


def send_question(conn, emp: dict, run: dict, q: dict, *, opening=False, reask=False) -> int:
    defn = survey_def(run["survey_key"])
    row = conn.execute("UPDATE survey_runs SET current_q=%s, nonce=nonce+1 WHERE id=%s RETURNING nonce, started_at",
                       (q["id"], run["id"])).fetchone()
    nonce = row["nonce"]
    run["nonce"], run["current_q"] = nonce, q["id"]
    pend = (conn.execute("SELECT pending_input FROM employees WHERE id=%s", (emp["id"],)).fetchone() or {}).get("pending_input") or {}
    tag = run["id"] if row["started_at"] is None else None   # reminders count from the first accepted message
    if reask and pend.get("run") == run["id"] and pend.get("qid") == q["id"] and pend.get("type") in ("survey_comment", "survey_own"):
        # the question was answered and we still wait for its comment / own option: ask for that again
        pend["nonce"] = nonce
        conn.execute("UPDATE employees SET pending_input=%s WHERE id=%s", (J(pend), emp["id"]))
        buttons = []
        if pend.get("personal"):
            buttons.append([{"text": "Обсудить лично", "payload": f"pl:{run['id']}:{nonce}"}])
        if pend["type"] == "survey_comment":
            buttons.append([{"text": "Пропустить", "payload": f"sk:{run['id']}:{nonce}"}])
        return _say(conn, emp, pend.get("prompt") or "Жду твой ответ одним сообщением.", buttons=buttons, survey_run_id=tag)
    if pend.get("run") == run["id"] and pend.get("type", "").startswith("survey_"):
        conn.execute("UPDATE employees SET pending_input=NULL WHERE id=%s", (emp["id"],))
    idx = [x["id"] for x in defn["questions"]].index(q["id"]) + 1
    total = len(defn["questions"])
    head = f"Вопрос {idx} из {total}\n" if total > 1 else ""
    hint = ""
    if q["type"] == "text":
        hint = "\n\n(Напиши ответ одним сообщением.)" if emp.get("address_form") != "vy" else "\n\n(Напишите ответ одним сообщением.)"
        conn.execute("UPDATE employees SET pending_input=%s WHERE id=%s",
                     (J({"type": "survey_text", "run": run["id"], "qid": q["id"], "nonce": nonce}), emp["id"]))
    draft = (run.get("draft") or {}).get(q["id"], []) if q["type"] == "multi" else None
    ob = _say(conn, emp, head + personalize(emp, q["text"], q.get("text_vy")) + hint,
              buttons=_buttons(emp, run, q, nonce, draft),
              survey_run_id=tag)
    if q["type"] == "multi":
        conn.execute("UPDATE survey_runs SET draft = jsonb_set(draft, '{_qmsg}', to_jsonb(%s::bigint)) WHERE id=%s",
                     (ob, run["id"]))
    return ob


def on_opening_accepted(conn, run_id: int) -> None:
    s = get_settings()
    run = conn.execute("""SELECT r.*, e.timezone FROM survey_runs r JOIN employees e ON e.id=r.employee_id
                          WHERE r.id=%s""", (run_id,)).fetchone()
    if run and run["started_at"] is None:
        now = datetime.now(timezone.utc)
        conn.execute("UPDATE survey_runs SET started_at=%s, next_reminder_at=%s WHERE id=%s",
                     (now, next_local(now, run["timezone"], s.reminder_time, 1), run_id))


def _active_run(conn, emp_id: int, run_id: int, nonce: int | None) -> dict | None:
    run = conn.execute("SELECT * FROM survey_runs WHERE id=%s AND employee_id=%s FOR UPDATE",
                       (run_id, emp_id)).fetchone()
    if not run or run["status"] != "active":
        return None
    if nonce is not None and run["nonce"] != nonce:
        return None
    return run


def _store(conn, emp, run, q, *, option_ids=(), labels=(), score=None, text=None, message_id=None) -> int:
    row = conn.execute(
        """INSERT INTO survey_answers(run_id, employee_id, question_id, question_text, qtype, option_ids,
                                      option_labels, score, text, source_message_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (run_id, question_id) DO UPDATE SET option_ids=EXCLUDED.option_ids,
               option_labels=EXCLUDED.option_labels, score=EXCLUDED.score,
               text=COALESCE(EXCLUDED.text, survey_answers.text), updated_at=now()
           RETURNING id""",
        (run["id"], emp["id"], q["id"], q["text"], q["type"], list(option_ids), list(labels), score, text,
         message_id)).fetchone()
    return row["id"]


def answer_option(conn, emp: dict, run_id: int, nonce: int | None, value: str, *, message_id=None) -> str:
    """single / yes_no / scale answer (button or unambiguous text)."""
    from ..cases import open_case
    run = _active_run(conn, emp["id"], run_id, nonce)
    if not run:
        return "stale"
    defn = survey_def(run["survey_key"])
    q = _q(defn, run["current_q"])
    if q["type"] == "scale":
        try:
            score = int(value)
        except ValueError:
            return "stale"
        if not 1 <= score <= 10:
            return "stale"
        aid = _store(conn, emp, run, q, score=score, labels=[str(score)], message_id=message_id)
        if message_id is None:
            conn.execute("INSERT INTO messages(employee_id, direction, author_kind, text, payload) VALUES (%s,'in','employee',%s,%s)",
                         (emp["id"], str(score), J({"survey": run["survey_key"], "q": q["id"]})))
        comment = q.get("comment")
        if comment and comment["if"] == "score_lt" and score < comment["value"]:
            return _ask_comment(conn, emp, run, q, comment, aid)
        return advance(conn, emp, run)
    opt = next((o for o in q.get("options", []) if o["id"] == value), None)
    if not opt or q["type"] not in ("single", "yes_no"):
        return "stale"
    if message_id is None:
        conn.execute("INSERT INTO messages(employee_id, direction, author_kind, text, payload) VALUES (%s,'in','employee',%s,%s)",
                     (emp["id"], opt["label"], J({"survey": run["survey_key"], "q": q["id"], "option": opt["id"]})))
    if opt.get("own"):
        conn.execute("UPDATE employees SET pending_input=%s WHERE id=%s",
                     (J({"type": "survey_own", "run": run["id"], "qid": q["id"], "option": opt["id"],
                         "nonce": run["nonce"], "prompt": "Напиши свой вариант одним сообщением."}), emp["id"]))
        _say(conn, emp, "Напиши свой вариант одним сообщением.", "Напишите свой вариант одним сообщением.")
        return "ok"
    aid = _store(conn, emp, run, q, option_ids=[opt["id"]], labels=[opt["label"]], message_id=message_id)
    if opt.get("signal"):
        sig = opt["signal"]
        if sig.get("case"):
            open_case(conn, emp, reason="feedback_signal", priority=sig["priority"], topic=sig["topic"],
                      question=f"{q['text']} — «{opt['label']}»", root_message_id=None,
                      dedupe_key=f"survey:{run['id']}:{q['id']}:signal",
                      context={"survey": run["survey_key"], "question": q["text"], "answer": opt["label"],
                               "rule": sig.get("reason")})
        conn.execute("""INSERT INTO analyses(employee_id, source_type, source_id, analyzer_version, status, sentiment,
                        topics, signals, summary, priority, requires_hr, model)
                        VALUES (%s,'survey_answer',%s,'rules-v1','rule_only','unknown',%s,%s,%s,%s,%s,'rules')
                        ON CONFLICT DO NOTHING""",
                     (emp["id"], aid, [sig["topic"]], J([{"type": sig["topic"], "quote": opt["label"]}]),
                      sig.get("reason", ""), sig["priority"], bool(sig.get("case"))))
    for key in ("reply", "after"):
        if opt.get(key):
            _say(conn, emp, opt[key]["text"], opt[key].get("text_vy"))
    comment = q.get("comment") or opt.get("comment")
    if opt.get("comment"):
        comment = opt["comment"]
    if comment and (comment["if"] == "always" or (comment["if"] == "option_in" and opt["id"] in comment["value"])):
        return _ask_comment(conn, emp, run, q, comment, aid)
    return advance(conn, emp, run)


def toggle_multi(conn, emp: dict, run_id: int, nonce: int, opt_id: str) -> str:
    run = _active_run(conn, emp["id"], run_id, nonce)
    if not run:
        return "stale"
    q = _q(survey_def(run["survey_key"]), run["current_q"])
    if q["type"] != "multi" or opt_id not in {o["id"] for o in q["options"]}:
        return "stale"
    draft = dict(run["draft"] or {})
    sel = list(draft.get(q["id"], []))
    exclusive = {o["id"] for o in q["options"] if o.get("exclusive")}
    if opt_id in sel:
        sel.remove(opt_id)
    elif opt_id in exclusive:
        sel = [opt_id]
    else:
        sel = [x for x in sel if x not in exclusive] + [opt_id]
    draft[q["id"]] = sel
    conn.execute("UPDATE survey_runs SET draft=%s WHERE id=%s", (J(draft), run["id"]))
    run["draft"] = draft
    labels = [o["label"] for o in q["options"] if o["id"] in sel]
    text = f"{personalize(emp, q['text'], q.get('text_vy'))}\n\nВыбрано: {', '.join(labels) or '—'}"
    queue_to_employee(conn, emp, text, author_kind="bot_script", source="scenario",
                      buttons=_buttons(emp, run, q, nonce, sel), edit_of_outbox=draft.get("_qmsg"),
                      payload={"survey_draft": True})
    return "ok"


def confirm_multi(conn, emp: dict, run_id: int, nonce: int) -> str:
    run = _active_run(conn, emp["id"], run_id, nonce)
    if not run:
        return "stale"
    q = _q(survey_def(run["survey_key"]), run["current_q"])
    sel = (run["draft"] or {}).get(q["id"], [])
    if not sel:
        _say(conn, emp, "Выбери, пожалуйста, хотя бы один вариант и нажми «Готово».",
             "Выберите, пожалуйста, хотя бы один вариант и нажмите «Готово».")
        return "ok"
    opts = [o for o in q["options"] if o["id"] in sel]
    aid = _store(conn, emp, run, q, option_ids=[o["id"] for o in opts], labels=[o["label"] for o in opts])
    conn.execute("INSERT INTO messages(employee_id, direction, author_kind, text, payload) VALUES (%s,'in','employee',%s,%s)",
                 (emp["id"], ", ".join(o["label"] for o in opts), J({"survey": run["survey_key"], "q": q["id"]})))
    for o in opts:
        if o.get("after"):
            _say(conn, emp, o["after"]["text"], o["after"].get("text_vy"))
        if o.get("comment"):
            return _ask_comment(conn, emp, run, q, o["comment"], aid)
    return advance(conn, emp, run)


def _ask_comment(conn, emp, run, q, comment: dict, answer_id: int) -> str:
    conn.execute("UPDATE employees SET pending_input=%s WHERE id=%s",
                 (J({"type": "survey_comment", "run": run["id"], "qid": q["id"], "answer_id": answer_id,
                     "nonce": run["nonce"], "escalate": comment.get("escalate"), "topic": q.get("topic") or comment.get("topic"),
                     "optional": comment.get("optional", True), "prompt": comment["prompt"],
                     "personal": bool(comment.get("personal_button"))}), emp["id"]))
    buttons = []
    if comment.get("personal_button"):
        buttons.append([{"text": "Обсудить лично", "payload": f"pl:{run['id']}:{run['nonce']}"}])
    buttons.append([{"text": "Пропустить", "payload": f"sk:{run['id']}:{run['nonce']}"}])
    _say(conn, emp, comment["prompt"], buttons=buttons)
    return "ok"


def on_text(conn, emp: dict, pending: dict, text: str, message_id: int) -> str:
    """Free text bound to the current survey input (text question / own option / comment)."""
    from ..cases import open_case
    run = _active_run(conn, emp["id"], pending["run"], pending.get("nonce"))
    if not run:
        conn.execute("UPDATE employees SET pending_input=NULL WHERE id=%s", (emp["id"],))
        emp = conn.execute("SELECT * FROM employees WHERE id=%s", (emp["id"],)).fetchone()
        from ..dialog import route
        route(conn, emp, text, message_id, "", skip_pending=True)  # never drop the message
        return "rerouted"
    defn = survey_def(run["survey_key"])
    q = _q(defn, pending["qid"])
    conn.execute("UPDATE employees SET pending_input=NULL WHERE id=%s", (emp["id"],))
    conn.execute("UPDATE messages SET payload = payload || %s WHERE id=%s",
                 (J({"survey": run["survey_key"], "q": q["id"], "kind": pending["type"]}), message_id))
    if pending["type"] == "survey_text":
        aid = _store(conn, emp, run, q, text=text, message_id=message_id)
        enqueue(conn, "analyze", {"source_type": "survey_answer", "source_id": aid, "maybe_case": True},
                dedupe_key=f"analyze:sa:{aid}:{message_id}")
    elif pending["type"] == "survey_own":
        opt = next(o for o in q["options"] if o["id"] == pending["option"])
        aid = _store(conn, emp, run, q, option_ids=[opt["id"]], labels=[opt["label"]], text=text, message_id=message_id)
        enqueue(conn, "analyze", {"source_type": "survey_answer", "source_id": aid, "maybe_case": True},
                dedupe_key=f"analyze:sa:{aid}:{message_id}")
    else:  # survey_comment
        conn.execute("UPDATE survey_answers SET comment=%s, updated_at=now() WHERE id=%s", (text, pending["answer_id"]))
        aid = pending["answer_id"]
        if pending.get("escalate"):
            open_case(conn, emp, reason="feedback_signal", priority=pending["escalate"],
                      topic=pending.get("topic") or "other", question=text, root_message_id=message_id,
                      dedupe_key=f"survey:{run['id']}:{q['id']}:comment",
                      context={"survey": run["survey_key"], "question": q["text"]})
        enqueue(conn, "analyze", {"source_type": "survey_answer", "source_id": aid,
                                  "maybe_case": not pending.get("escalate")},
                dedupe_key=f"analyze:sa:{aid}:{message_id}")
        _say(conn, emp, "Спасибо, записал.", "Спасибо, записал.")
    return advance(conn, emp, run, after=q["id"])


def skip_comment(conn, emp: dict, run_id: int, nonce: int, personal=False) -> str:
    from ..cases import open_case
    run = _active_run(conn, emp["id"], run_id, nonce)
    pend = emp.get("pending_input") or {}
    if not run or pend.get("type") != "survey_comment" or pend.get("run") != run_id:
        return "stale"
    conn.execute("UPDATE employees SET pending_input=NULL WHERE id=%s", (emp["id"],))
    q = _q(survey_def(run["survey_key"]), pend["qid"])
    if personal:
        open_case(conn, emp, reason="human_request", priority=pend.get("escalate") or "medium",
                  topic=pend.get("topic") or "retention",
                  question=f"Сотрудник готов обсудить лично: «{q['text']}»",
                  dedupe_key=f"survey:{run_id}:{pend['qid']}:personal",
                  context={"survey": run["survey_key"], "question": q["text"]})
        _say(conn, emp, "Хорошо, передам коллеге из HR-службы — он свяжется с тобой здесь, в этом чате.",
             "Хорошо, передам коллеге из HR-службы — он свяжется с вами здесь, в этом чате.")
    return advance(conn, emp, run, after=pend["qid"])


def advance(conn, emp: dict, run: dict, after: str | None = None) -> str:
    defn = survey_def(run["survey_key"])
    ids = [q["id"] for q in defn["questions"]]
    cur = after or run["current_q"]
    i = ids.index(cur)
    if i + 1 < len(ids):
        send_question(conn, emp, run, defn["questions"][i + 1])
        return "ok"
    return complete(conn, emp, run)


def complete(conn, emp: dict, run: dict) -> str:
    from .engine import find_material, on_survey_finished
    defn = survey_def(run["survey_key"])
    conn.execute("""UPDATE survey_runs SET status='completed', completed_at=now(), next_reminder_at=NULL,
                    current_q=NULL WHERE id=%s""", (run["id"],))
    for r in defn.get("outro_first", []) + defn.get("outro", []):
        _say(conn, emp, r["text"], r.get("text_vy"))
    fw = defn.get("farewell")
    if fw:
        m = find_material(conn, emp, fw.get("image", ""))
        _say(conn, emp, fw["text"], fw.get("text_vy"), materials=[m["id"]] if m else None,
             buttons=[[{"text": o, "payload": f"fw:{run['id']}:{k}"}] for k, o in enumerate(fw["options"])])
        conn.execute("UPDATE employees SET status='completed', updated_at=now() WHERE id=%s AND status='active'",
                     (emp["id"],))
    on_survey_finished(conn, emp, run["event_id"])
    nxt = conn.execute("""SELECT * FROM survey_runs WHERE employee_id=%s AND status='queued'
                          ORDER BY id LIMIT 1""", (emp["id"],)).fetchone()
    if nxt:
        _activate(conn, emp, nxt)
    return "ok"


def current_question(conn, emp_id: int) -> tuple[dict, dict] | None:
    run = conn.execute("SELECT * FROM survey_runs WHERE employee_id=%s AND status='active'", (emp_id,)).fetchone()
    if not run or not run["current_q"]:
        return None
    return run, _q(survey_def(run["survey_key"]), run["current_q"])


def resend_current(conn, emp: dict) -> bool:
    cur = current_question(conn, emp["id"])
    if not cur:
        return False
    run, q = cur
    send_question(conn, emp, run, q, reask=True)
    return True


# ---------------------------------------------------------------- reminders
def send_due_reminders(conn) -> int:
    s = get_settings()
    sc = scenario()["survey_reminder"]
    rows = conn.execute(
        """SELECT r.*, e.timezone FROM survey_runs r JOIN employees e ON e.id = r.employee_id
           WHERE r.status='active' AND r.next_reminder_at <= now() AND r.reminders_sent < %s
             AND e.dialog_mode <> 'hr_active' AND e.status NOT IN ('paused','deactivated')
           FOR UPDATE OF r SKIP LOCKED""", (sc["max"],)).fetchall()
    for run in rows:
        emp = conn.execute("SELECT * FROM employees WHERE id=%s", (run["employee_id"],)).fetchone()
        _say(conn, emp, sc["text"], sc.get("text_vy"))
        q = _q(survey_def(run["survey_key"]), run["current_q"])
        send_question(conn, emp, run, q, reask=True)
        sent = run["reminders_sent"] + 1
        nxt = None
        if sent < sc["max"]:
            nxt = next_local(run["started_at"], run["timezone"], s.reminder_time, 2)
            if nxt <= datetime.now(timezone.utc):
                nxt = next_local(datetime.now(timezone.utc), run["timezone"], s.reminder_time, 1)
        conn.execute("UPDATE survey_runs SET reminders_sent=%s, next_reminder_at=%s WHERE id=%s",
                     (sent, nxt, run["id"]))
    return len(rows)


def shift_reminders_after_pause(conn, emp: dict) -> None:
    """Documented rule: reminders that fell into an HR conversation are not sent in a burst;
    the next one is moved to the next day after the conversation ended."""
    s = get_settings()
    now = datetime.now(timezone.utc)
    conn.execute("""UPDATE survey_runs SET next_reminder_at=%s
                    WHERE employee_id=%s AND status='active' AND next_reminder_at < now()""",
                 (next_local(now, emp["timezone"], s.reminder_time, 1), emp["id"]))
