"""Inbound MAX updates -> one dialog with Корсик.

Routing order for an employee message (section 7.1 of the spec):
  1. hr_active            -> the message belongs to the HR conversation; bot stays silent;
  2. commands             -> /start, /food, /hr (=internal human request);
  3. pending input        -> survey text / own option / comment / intro; a question-like
                             message gets a short "answer or separate question?" clarification;
  4. explicit human request (rules, works without LLM) -> case + one calm message;
  5. button-equivalent text for open questions ("да", "8") -> exactly one target, else clarify;
  6. personal status "что мне осталось?" -> from real events;
  7. everything else      -> background job: analysis-first for problems, then RAG.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

from .analysis import is_human_request, is_personal_data_question, rule_signals
from .cases import attach_to_case, open_case
from .db import J, enqueue, new_request_id, tx
from .outbound import personalize, queue_to_employee, queue_to_user
from .scenario import engine, survey
from .transport import get_transport

log = logging.getLogger(__name__)

QUESTION_START = re.compile(r"^(как|где|что|когда|сколько|можно|кто|почему|зачем|какой|какая|какие|каков|куда|"
                            r"подскажи|подскажите|расскажи|расскажите|есть ли|нужно ли|надо ли|могу ли|положен)\b", re.I)
YES = {"да", "ага", "конечно", "yes", "угу", "давай", "да!", "хочу"}
NO = {"нет", "не", "no", "неа", "не надо", "нет спасибо"}
GREETING = re.compile(r"^(привет|здравствуй|здравствуйте|добрый день|доброе утро|добрый вечер|спасибо|благодарю|ок|окей|хорошо|понятно|ясно)[\s!.)]*$", re.I)
STATUS_Q = re.compile(r"(что|сколько)\W+(?:\w+\W+){0,2}(мне )?(осталось|ещ[её] предстоит)|мой прогресс|что дальше по плану", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^\wё ]", "", s.lower().replace("ё", "е")).strip()


def is_question_like(text: str) -> bool:
    return "?" in text or bool(QUESTION_START.search(text.strip()))


def say(conn, emp, text, text_vy=None, *, source="system", **kw):
    return queue_to_employee(conn, emp, personalize(emp, text, text_vy or text), author_kind="bot_system",
                             source=source, **kw)


# ---------------------------------------------------------------- MAX update normalization
def normalize(update: dict) -> dict | None:
    kind = update.get("update_type")
    if kind == "bot_started":
        user = update.get("user") or {}
        if user.get("is_bot"):
            return None
        return {"kind": "start", "user_id": str(user.get("user_id")), "chat_id": str(update.get("chat_id")),
                "payload": (update.get("payload") or "")[:256], "name": user.get("name") or ""}
    msg = update.get("message") or {}
    rec = msg.get("recipient") or {}
    if kind == "message_callback":
        cb = update.get("callback") or {}
        user = cb.get("user") or {}
        return {"kind": "callback", "user_id": str(user.get("user_id")), "chat_id": str(rec.get("chat_id") or ""),
                "callback_id": cb.get("callback_id"), "payload": cb.get("payload") or ""}
    if kind == "message_created":
        if rec.get("chat_type") not in (None, "dialog"):
            return None  # HR context never goes to group chats
        sender = msg.get("sender") or {}
        if sender.get("is_bot"):
            return None
        body = msg.get("body") or {}
        atts = []
        for a in body.get("attachments") or []:
            if a.get("type") in ("image", "file", "video"):
                p = a.get("payload") or {}
                atts.append({"type": a["type"], "ref": p.get("token") or p.get("url"), "url": p.get("url"),
                             "name": a.get("filename", "")})
        return {"kind": "message", "user_id": str(sender.get("user_id")), "chat_id": str(rec.get("chat_id")),
                "text": (body.get("text") or "").strip(), "mid": body.get("mid"), "attachments": atts}
    return None


def provider_event_id(update: dict) -> str:
    kind = update.get("update_type", "?")
    if kind == "message_callback":
        return "cb:" + str((update.get("callback") or {}).get("callback_id"))
    if kind == "message_created":
        return "msg:" + str(((update.get("message") or {}).get("body") or {}).get("mid"))
    raw = f"{kind}:{update.get('timestamp')}:{(update.get('user') or {}).get('user_id')}:{update.get('payload')}"
    return "upd:" + hashlib.sha256(raw.encode()).hexdigest()[:40]


# ---------------------------------------------------------------- processing entry point
import threading
_local = threading.local()


def _defer_direct(chat_id: str, text: str) -> None:
    """Replies to chats without an employee card are sent after the transaction commits."""
    _local.direct.append((chat_id, text))


def process_inbound(inbound_id: int) -> None:
    from .db import one as db_one
    row = db_one("SELECT payload, status, request_id FROM inbound_events WHERE id=%s", (inbound_id,))
    if not row or row["status"] != "received":
        return
    u = normalize(row["payload"])
    rid = row["request_id"]
    if u and u["kind"] == "callback" and u.get("callback_id"):
        try:  # acknowledgement is best effort and never inside a DB transaction
            get_transport().answer_callback(u["callback_id"])
        except Exception as e:
            log.info("callback ack failed request_id=%s %s", rid, type(e).__name__)
    _local.direct = []
    with tx() as c:
        ev = c.execute("SELECT * FROM inbound_events WHERE id=%s FOR UPDATE SKIP LOCKED", (inbound_id,)).fetchone()
        if not ev or ev["status"] != "received":
            return
        if u is None:
            c.execute("UPDATE inbound_events SET status='ignored', processed_at=now() WHERE id=%s", (inbound_id,))
            return
        if u["kind"] == "start":
            handle_start(c, u, rid)
        else:
            emp = c.execute("SELECT * FROM employees WHERE max_user_id=%s FOR UPDATE", (u["user_id"],)).fetchone()
            if emp:
                if emp["max_chat_id"] != u["chat_id"] and u["chat_id"]:
                    c.execute("UPDATE employees SET max_chat_id=%s WHERE id=%s", (u["chat_id"], emp["id"]))
                    emp["max_chat_id"] = u["chat_id"]
                if u["kind"] == "callback":
                    handle_callback(c, emp, u["payload"], rid)
                else:
                    handle_message(c, emp, u, rid)
            else:
                staff = c.execute("SELECT * FROM users WHERE max_user_id=%s", (u["user_id"],)).fetchone()
                if staff:
                    queue_to_user(c, staff, "Это служебный канал уведомлений Корсика. Отвечать сотрудникам "
                                            "нужно из кабинета HR.")
                else:
                    _reply_unknown(c, u)
        c.execute("UPDATE inbound_events SET status='processed', processed_at=now() WHERE id=%s", (inbound_id,))
    for chat_id, text in _local.direct:
        try:
            get_transport().send(chat_id, {"text": text})
        except Exception as e:
            log.info("direct reply failed request_id=%s %s", rid, type(e).__name__)


def _reply_unknown(c, u: dict) -> None:
    """Unknown MAX user: no card is created from chat text."""
    _defer_direct(u["chat_id"], "Здравствуйте! Я Корсик, цифровой помощник HR-службы Группы ПОЛИПЛАСТИК. Чтобы "
                                "начать, откройте персональную ссылку-приглашение, которую вам передал специалист HR.")


# ---------------------------------------------------------------- invites
def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def handle_start(c, u: dict, rid: str) -> None:
    payload = u.get("payload") or ""
    emp = c.execute("SELECT * FROM employees WHERE max_user_id=%s FOR UPDATE", (u["user_id"],)).fetchone()
    if not payload:
        if emp:
            say(c, emp, "Я на связи! Пиши, если появятся вопросы.", "Я на связи! Пишите, если появятся вопросы.")
            return
        _reply_unknown(c, u)
        return
    inv = c.execute("SELECT * FROM invites WHERE token_hash=%s FOR UPDATE", (token_hash(payload),)).fetchone()
    now = datetime.now(timezone.utc)

    def direct(text):
        _defer_direct(u["chat_id"], text)

    if not inv or inv["revoked_at"] or inv["expires_at"] < now:
        if emp:
            say(c, emp, "Эта ссылка уже неактивна, но ты уже подключен — продолжаем здесь.",
                "Эта ссылка уже неактивна, но вы уже подключены — продолжаем здесь.")
        else:
            direct("Ссылка-приглашение недействительна или истекла. Попросите специалиста HR прислать новую.")
        return
    if inv["used_at"] and inv["used_by_max"] != u["user_id"]:
        direct("Эта ссылка-приглашение уже использована другим пользователем. Обратитесь к специалисту HR.")
        return
    if inv["kind"] == "manager":
        other = c.execute("SELECT id FROM users WHERE max_user_id=%s AND id<>%s", (u["user_id"], inv["user_id"])).fetchone()
        if other:
            direct("Этот аккаунт MAX уже привязан к другой учетной записи.")
            return
        c.execute("UPDATE users SET max_user_id=%s, max_chat_id=%s, max_linked_at=now() WHERE id=%s",
                  (u["user_id"], u["chat_id"], inv["user_id"]))
        c.execute("UPDATE invites SET used_at=COALESCE(used_at, now()), used_by_max=%s WHERE id=%s", (u["user_id"], inv["id"]))
        user = c.execute("SELECT * FROM users WHERE id=%s", (inv["user_id"],)).fetchone()
        queue_to_user(c, user, "Готово: служебные уведомления Корсика будут приходить в этот чат.")
        return
    # employee invite
    if emp and emp["id"] != inv["employee_id"]:
        direct("Этот аккаунт MAX уже привязан к другой карточке сотрудника. Обратитесь к специалисту HR.")
        return
    target = c.execute("SELECT * FROM employees WHERE id=%s FOR UPDATE", (inv["employee_id"],)).fetchone()
    if target["max_user_id"] and target["max_user_id"] != u["user_id"]:
        direct("Эта карточка уже привязана к другому аккаунту MAX. Обратитесь к специалисту HR.")
        return
    first_time = target["max_user_id"] is None
    c.execute("""UPDATE employees SET max_user_id=%s, max_chat_id=%s, linked_at=COALESCE(linked_at, now()),
                 updated_at=now() WHERE id=%s""", (u["user_id"], u["chat_id"], target["id"]))
    c.execute("UPDATE invites SET used_at=COALESCE(used_at, now()), used_by_max=%s WHERE id=%s", (u["user_id"], inv["id"]))
    emp = c.execute("SELECT * FROM employees WHERE id=%s", (target["id"],)).fetchone()
    if not first_time:
        say(c, emp, "С возвращением! Продолжаем с того же места.", "С возвращением! Продолжаем с того же места.")
        survey.resend_current(c, emp)
        return
    c.execute("""INSERT INTO events(employee_id, step_key, scenario_code, title, source_rows, due_at)
                 VALUES (%s,'preonboarding',%s,'Преонбординг','3-15', now()) ON CONFLICT DO NOTHING""",
              (emp["id"], engine.scenario()["code"]))
    c.execute("UPDATE employees SET preonboarding_state='running' WHERE id=%s", (emp["id"],))
    if emp["status"] == "active" and emp["start_date"]:
        engine.materialize_events(c, emp)
    c.execute("NOTIFY korsik_jobs")


# ---------------------------------------------------------------- callbacks
def handle_callback(c, emp: dict, payload: str, rid: str) -> None:
    parts = payload.split(":")
    kind = parts[0]
    res = "stale"
    try:
        if emp["dialog_mode"] == "hr_active":
            # buttons of automatic flows are paused while HR owns the dialog
            msg = c.execute("INSERT INTO messages(employee_id, direction, author_kind, text, payload) VALUES (%s,'in','employee',%s,%s) RETURNING id",
                            (emp["id"], "[нажата кнопка]", J({"callback": payload}))).fetchone()
            attach_to_case(c, emp, msg["id"])
            return
        if kind == "a":
            res = engine.on_ask_reply(c, emp, int(parts[1]), parts[2], parts[3], rid)
        elif kind == "s":
            res = survey.answer_option(c, emp, int(parts[1]), int(parts[2]), parts[3])
        elif kind == "m":
            res = survey.toggle_multi(c, emp, int(parts[1]), int(parts[2]), parts[3])
        elif kind == "md":
            res = survey.confirm_multi(c, emp, int(parts[1]), int(parts[2]))
        elif kind == "sk":
            res = survey.skip_comment(c, emp, int(parts[1]), int(parts[2]))
        elif kind == "pl":
            res = survey.skip_comment(c, emp, int(parts[1]), int(parts[2]), personal=True)
        elif kind == "ix":
            if (emp.get("pending_input") or {}).get("type") == "intro":
                c.execute("UPDATE employees SET pending_input=NULL WHERE id=%s", (emp["id"],))
                say(c, emp, "Хорошо! Когда будешь готов, пришли фото или напиши «О себе: …» — я сохраню это для HR.",
                    "Хорошо! Когда будете готовы, пришлите фото или напишите «О себе: …» — я сохраню это для HR.")
                res = "ok"
        elif kind == "cl":
            res = resolve_clarification(c, emp, int(parts[1]), parts[2], rid)
        elif kind == "fw":
            c.execute("INSERT INTO messages(employee_id, direction, author_kind, text) VALUES (%s,'in','employee','[прощание]')",
                      (emp["id"],))
            res = "ok"
    except (ValueError, IndexError):
        res = "stale"
    if res == "stale":
        log.info("stale callback request_id=%s payload=%s", rid, payload[:40])


# ---------------------------------------------------------------- messages
def handle_message(c, emp: dict, u: dict, rid: str) -> None:
    text = u.get("text") or ""
    atts = u.get("attachments") or []
    msg = c.execute("""INSERT INTO messages(employee_id, direction, author_kind, text, payload, provider_message_id,
                       dialog_mode, dialog_revision, request_id) VALUES (%s,'in','employee',%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (emp["id"], text, J({"attachments": atts}), u.get("mid"), emp["dialog_mode"],
                     emp["dialog_revision"], rid)).fetchone()
    mid = msg["id"]
    route(c, emp, text, mid, rid, attachments=atts)


def route(c, emp: dict, text: str, mid: int, rid: str, *, attachments=None, skip_pending=False) -> str:
    attachments = attachments or []
    # 1. HR owns the dialog
    if emp["dialog_mode"] == "hr_active":
        attach_to_case(c, emp, mid)
        return "to_hr"
    # Employee's own plan is a global file request; keep survey/comment state intact.
    from .personal_plan import handle_plan_request
    if handle_plan_request(c, emp, text, mid, rid):
        return "personal_plan"
    low = text.strip().lower()
    # 2. commands
    if low.startswith("/start"):
        say(c, emp, "Я на связи! Пиши, если появятся вопросы.", "Я на связи! Пишите, если появятся вопросы.")
        return "start"
    if low.startswith("/food"):
        engine.send_material(engine.Ctx(c, emp, {"id": 0, "state": {}}, rid, user_initiated=True),
                             {"type": "material", "key": "food", "row": "25", "text": "Приятного аппетита!",
                              "text_vy": "Приятного аппетита!"})
        return "food"
    pending = None if skip_pending else emp.get("pending_input")
    if pending and pending.get("type") in ("comment", "intro") and _expired(pending):
        c.execute("UPDATE employees SET pending_input=NULL WHERE id=%s", (emp["id"],))
        pending = None
    if pending and text and GREETING.match(text) and not attachments:
        if pending.get("type") in ("comment", "intro"):
            pending = None                          # "спасибо"/"ок" is not a comment or an intro
        elif pending.get("type") == "survey_comment" and pending.get("optional"):
            survey.skip_comment(c, emp, pending["run"], pending.get("nonce"))
            return "comment_skipped"
    if not pending and _intro_later(c, emp, text, attachments):
        pending = {"type": "intro", "event_id": _intro_later(c, emp, text, attachments)}
        if text.lower().startswith("о себе"):
            text = text.split(":", 1)[-1].strip() if ":" in text else text[7:].strip()
    # 3. pending input bound to a concrete step
    if pending and pending.get("type") in ("survey_text", "survey_own", "survey_comment", "comment", "intro"):
        if pending["type"] == "intro" and attachments and not text:
            _save_intro(c, emp, pending, None, attachments)
            return "intro"
        if text and is_question_like(text) and not is_human_request(text):
            return ask_clarification(c, emp, mid, pending, text)
        return apply_pending(c, emp, pending, text, mid, attachments, rid)
    # 4. explicit human request (rule; no LLM required)
    if text and is_human_request(text):
        return human_request(c, emp, text, mid)
    # 5. button-equivalent answers
    if text and not is_question_like(text):
        cands = text_candidates(c, emp, text)
        if len(cands) == 1:
            return apply_candidate(c, emp, cands[0], mid, rid)
        if len(cands) > 1:
            return ask_target(c, emp, mid, text, cands)
    # 6. personal status
    if text and STATUS_Q.search(text):
        status_reply(c, emp)
        return "status"
    if not text and attachments:
        say(c, emp, "Получил файл. Если это ответ на мой вопрос — напиши, пожалуйста, пару слов текстом.",
            "Получил файл. Если это ответ на мой вопрос — напишите, пожалуйста, пару слов текстом.")
        return "attachment"
    # waiting_hr: additions (not new questions) join the open case without another "уточню"
    if emp["dialog_mode"] == "waiting_hr" and emp.get("active_case_id") and not is_question_like(text):
        attach_to_case(c, emp, mid)
        last = c.execute("""SELECT text FROM messages WHERE employee_id=%s AND direction='out' ORDER BY id DESC LIMIT 1""",
                         (emp["id"],)).fetchone()
        ack = ("Добавил это к твоему вопросу — специалист HR-службы увидит.",
               "Добавил это к вашему вопросу — специалист HR-службы увидит.")
        if not last or last["text"] not in ack:
            say(c, emp, ack[0], ack[1])
        return "case_addition"
    if GREETING.match(text):
        say(c, emp, "Рад помочь! Если появятся вопросы о работе в компании — пиши.",
            "Рад помочь! Если появятся вопросы о работе в компании — пишите.")
        return "smalltalk"
    # 7. background: analysis-first + RAG
    enqueue(c, "free_message", {"employee_id": emp["id"], "message_id": mid, "revision": emp["dialog_revision"]},
            dedupe_key=f"free:{mid}", request_id=rid)
    return "queued"


def _expired(pending: dict, hours: int = 48) -> bool:
    at = pending.get("at")
    if not at:
        return False
    return datetime.now(timezone.utc) - datetime.fromisoformat(at) > __import__("datetime").timedelta(hours=hours)


def _intro_later(c, emp, text, attachments):
    """Intro (row 34) sent later: a photo or a message starting with «О себе» while the step is not done."""
    if not (any(a["type"] == "image" for a in attachments) or (text or "").lower().startswith("о себе")):
        return None
    ev = c.execute("SELECT id FROM events WHERE employee_id=%s AND step_key='d2_intro' AND status IN ('sent','waiting_reply')",
                   (emp["id"],)).fetchone()
    return ev and ev["id"]


def apply_pending(c, emp, pending, text, mid, attachments, rid) -> str:
    t = pending["type"]
    if t in ("survey_text", "survey_own", "survey_comment"):
        if not text.strip():
            say(c, emp, "Напиши, пожалуйста, ответ текстом.", "Напишите, пожалуйста, ответ текстом.")
            return "need_text"
        survey.on_text(c, emp, pending, text, mid)
        return "survey_text"
    if t == "comment":
        c.execute("UPDATE employees SET pending_input=NULL WHERE id=%s", (emp["id"],))
        open_case(c, emp, reason="feedback_signal", priority=pending.get("escalate") or "medium",
                  topic=pending.get("topic") or "other", question=text, root_message_id=mid,
                  dedupe_key=f"comment:{pending.get('event_id')}:{mid}",
                  context={"question": pending.get("question")})
        enqueue(c, "analyze", {"source_type": "message", "source_id": mid}, dedupe_key=f"analyze:msg:{mid}")
        say(c, emp, "Спасибо! Передал коллегам из HR-службы и вернусь с ответом.",
            "Спасибо! Передал коллегам из HR-службы и вернусь с ответом.")
        return "comment"
    if t == "intro":
        _save_intro(c, emp, pending, text, attachments)
        return "intro"
    return "unknown_pending"


def _save_intro(c, emp, pending, text, attachments) -> None:
    photo = next((a for a in attachments if a["type"] == "image"), None)
    c.execute("""UPDATE employees SET intro_text = CASE WHEN %s::text IS NULL THEN intro_text
                 ELSE concat_ws(E'\n', intro_text, %s::text) END,
                 intro_photo_ref = COALESCE(%s, intro_photo_ref), updated_at=now() WHERE id=%s""",
              (text, text, (photo or {}).get("url") or (photo or {}).get("ref"), emp["id"]))
    row = c.execute("SELECT intro_text, intro_photo_ref FROM employees WHERE id=%s", (emp["id"],)).fetchone()
    if row["intro_text"] and row["intro_photo_ref"]:
        c.execute("UPDATE employees SET pending_input=NULL WHERE id=%s", (emp["id"],))
        c.execute("UPDATE events SET status='done', updated_at=now() WHERE id=%s", (pending.get("event_id"),))
        say(c, emp, "Спасибо! Сохранил описание и фото. Представлять тебя коллегам будем только после "
                    "согласования с HR-службой.",
            "Спасибо! Сохранил описание и фото. Представлять вас коллегам будем только после согласования с HR-службой.")
    elif row["intro_text"]:
        say(c, emp, "Спасибо, описание сохранил! Пришли, пожалуйста, ещё фотографию.",
            "Спасибо, описание сохранил! Пришлите, пожалуйста, ещё фотографию.")
    else:
        say(c, emp, "Фото получил, спасибо! Напиши ещё пару предложений о своём опыте и навыках.",
            "Фото получил, спасибо! Напишите ещё пару предложений о своём опыте и навыках.")


# ---------------------------------------------------------------- clarifications
def ask_clarification(c, emp, mid: int, pending: dict, text: str) -> str:
    c.execute("UPDATE employees SET pending_input=%s WHERE id=%s",
              (J({"type": "clarify", "message_id": mid, "restore": pending}), emp["id"]))
    say(c, emp, "Уточню: это ответ на мой вопрос или отдельный вопрос?",
        "Уточню: это ответ на мой вопрос или отдельный вопрос?",
        buttons=[[{"text": "Это ответ", "payload": f"cl:{mid}:answer"}],
                 [{"text": "Отдельный вопрос", "payload": f"cl:{mid}:question"}]])
    return "clarify"


def ask_target(c, emp, mid: int, text: str, cands: list[dict]) -> str:
    c.execute("UPDATE employees SET pending_input=%s WHERE id=%s",
              (J({"type": "clarify_target", "message_id": mid, "candidates": cands}), emp["id"]))
    buttons = [[{"text": f"«{cand['question'][:50]}»", "payload": f"cl:{mid}:{i}"}] for i, cand in enumerate(cands)]
    buttons.append([{"text": "Это отдельное сообщение", "payload": f"cl:{mid}:question"}])
    say(c, emp, f"Уточню: «{text}» — это ответ на какой вопрос?", buttons=buttons)
    return "clarify"


def resolve_clarification(c, emp, mid: int, choice: str, rid: str) -> str:
    p = emp.get("pending_input") or {}
    if p.get("message_id") != mid or p.get("type") not in ("clarify", "clarify_target"):
        return "stale"
    msg = c.execute("SELECT * FROM messages WHERE id=%s AND employee_id=%s", (mid, emp["id"])).fetchone()
    if p["type"] == "clarify":
        restore = p["restore"]
        c.execute("UPDATE employees SET pending_input=%s WHERE id=%s", (J(restore), emp["id"]))
        emp["pending_input"] = restore
        if choice == "answer":
            apply_pending(c, emp, restore, msg["text"], mid, (msg["payload"] or {}).get("attachments") or [], rid)
        else:
            route(c, emp, msg["text"], mid, rid, skip_pending=True)
        return "ok"
    c.execute("UPDATE employees SET pending_input=NULL WHERE id=%s", (emp["id"],))
    emp["pending_input"] = None
    if choice == "question":
        enqueue(c, "free_message", {"employee_id": emp["id"], "message_id": mid, "revision": emp["dialog_revision"]},
                dedupe_key=f"free:{mid}", request_id=rid)
        return "ok"
    cand = p["candidates"][int(choice)]
    return apply_candidate(c, emp, cand, mid, rid)


def text_candidates(c, emp: dict, text: str) -> list[dict]:
    n = _norm(text)
    out = []
    for ask in engine.pending_asks(c, emp["id"]):
        for o in ask["options"]:
            ln = _norm(o["label"])
            if n == ln or (o["id"] == "yes" and n in YES) or (o["id"] == "no" and n in NO) or \
                    (ln.startswith(n) and len(n) >= 3 and n.split()[0] in ("да", "нет")):
                out.append({"kind": "ask", "event_id": ask["event_id"], "ask_id": ask["ask_id"], "option": o["id"],
                            "question": ask["text"][:80]})
                break
    cur = survey.current_question(c, emp["id"])
    if cur:
        run, q = cur
        if q["type"] == "scale" and re.fullmatch(r"(10|[1-9])", n):
            out.append({"kind": "survey", "run": run["id"], "nonce": run["nonce"], "value": n, "question": q["text"][:80]})
        elif q["type"] in ("single", "yes_no"):
            for o in q["options"]:
                ln = _norm(o["label"])
                if n == ln or (o["id"] == "yes" and n in YES) or (o["id"] == "no" and n in NO):
                    out.append({"kind": "survey", "run": run["id"], "nonce": run["nonce"], "value": o["id"],
                                "question": q["text"][:80]})
                    break
    return out


def apply_candidate(c, emp, cand: dict, mid: int, rid: str) -> str:
    if cand["kind"] == "ask":
        return engine.on_ask_reply(c, emp, cand["event_id"], cand["ask_id"], cand["option"], rid, message_id=mid)
    return survey.answer_option(c, emp, cand["run"], cand["nonce"], cand["value"], message_id=mid)


# ---------------------------------------------------------------- human request / status
def human_request(c, emp: dict, text: str, mid: int) -> str:
    case = open_case(c, emp, reason="human_request", priority="medium" if rule_signals(text) else "low",
                     topic=(rule_signals(text) or [{"topic": "other"}])[0]["topic"], question=text,
                     root_message_id=mid, dedupe_key=f"human:{mid}")
    enqueue(c, "analyze", {"source_type": "message", "source_id": mid}, dedupe_key=f"analyze:msg:{mid}")
    say(c, emp, "Хорошо, попрошу специалиста HR-службы ответить тебе лично — прямо здесь, в этом чате.",
        "Хорошо, попрошу специалиста HR-службы ответить вам лично — прямо здесь, в этом чате.", case_id=case["id"])
    return "human_request"


def status_reply(c, emp: dict) -> None:
    rows = c.execute("""SELECT title, due_at, status FROM events WHERE employee_id=%s AND NOT is_test
                        ORDER BY due_at""", (emp["id"],)).fetchall()
    done = [r for r in rows if r["status"] in ("sent", "done")]
    upcoming = [r for r in rows if r["status"] == "scheduled"][:3]
    active = survey.current_question(c, emp["id"])
    lines = [f"Пройдено этапов: {len(done)} из {len([r for r in rows if r['status'] != 'skipped'])}."]
    if active:
        lines.append(f"Сейчас ждёт ответа опрос «{survey.survey_def(active[0]['survey_key'])['title']}».")
    if upcoming:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(emp["timezone"])
        lines.append("Дальше по плану:")
        lines += [f"• {r['due_at'].astimezone(tz):%d.%m} — {r['title']}" for r in upcoming]
    elif not rows:
        lines.append("Расписание сопровождения ещё готовится HR-службой.")
    say(c, emp, "\n".join(lines))


def requeue_after_withdrawal(c, ob: dict) -> None:
    """A prepared answer cited a document that was withdrawn before sending: answer again."""
    msg = c.execute("SELECT payload FROM messages WHERE id=%s", (ob["message_id"],)).fetchone()
    reply_to = (msg["payload"] or {}).get("reply_to") if msg else None
    emp = c.execute("SELECT dialog_revision FROM employees WHERE id=%s", (ob["employee_id"],)).fetchone()
    if reply_to:
        enqueue(c, "free_message", {"employee_id": ob["employee_id"], "message_id": reply_to,
                                    "revision": emp["dialog_revision"], "retry_of": ob["id"]},
                dedupe_key=f"free:{reply_to}:retry:{ob['id']}")
