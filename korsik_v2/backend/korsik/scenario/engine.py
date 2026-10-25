"""Executable scenario: event materialization, deterministic step interpreter, asks, materials.

A step's actions run in order. An `ask` (button question) or `survey` pauses the step;
the resume pointer lives in events.state, so a restart or a closed chat loses nothing.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import get_settings
from ..db import J
from ..outbound import personalize, queue_to_employee
from .calendar import default_probation_end, due_at

log = logging.getLogger(__name__)
SCENARIO_FILE = Path(__file__).with_name("korsik_scenario.json")
INFO_ONLY = {"text", "material"}


@lru_cache
def scenario() -> dict:
    return json.loads(SCENARIO_FILE.read_text(encoding="utf-8"))


def register_scenario(conn) -> None:
    sc = scenario()
    conn.execute("""INSERT INTO scenario_versions(code, source_sha256, definition) VALUES (%s,%s,%s)
                    ON CONFLICT (code) DO NOTHING""", (sc["code"], sc["source"]["sha256"], J(sc)))


def step_by_key(key: str) -> dict | None:
    if key == "preonboarding":
        return {"key": "preonboarding", "rows": "3-15", "title": "Преонбординг", "actions": scenario()["preonboarding"]}
    if key.startswith("test:"):
        survey = key.split(":")[1]
        return {"key": key, "rows": "test", "title": f"Тестовый опрос {survey}",
                "actions": [{"type": "survey", "survey": survey, "test": True}]}
    return next((s for s in scenario()["steps"] if s["key"] == key), None)


# ---------------------------------------------------------------- applicability
def applies(item: dict, emp: dict, *, now_local_date: date | None = None) -> bool:
    tags = set(emp.get("tags") or [])
    item_tags = item.get("tags")
    if item_tags:
        if not (tags & set(item_tags)) and not (item.get("no_tag") and not tags):
            return False
    elif item.get("no_tag") and tags:
        return False
    if item.get("work_format") and emp.get("work_format") != item["work_format"]:
        return False
    if item.get("before_start") and emp.get("start_date") and now_local_date and now_local_date >= emp["start_date"]:
        return False
    return True


def local_today(emp: dict) -> date:
    return datetime.now(ZoneInfo(emp.get("timezone") or "Europe/Moscow")).date()


# ---------------------------------------------------------------- materialization
def materialize_events(conn, emp: dict, *, now: datetime | None = None) -> dict:
    """Create the employee's timeline. Idempotent (unique employee_id+step_key).
    Events already in the past at the moment of activation/late linking are created as
    'skipped' (late_start) so HR sees the gap and nothing is sent retroactively."""
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    sc = scenario()
    created, skipped = 0, 0
    for st in sc["steps"]:
        if not applies(st, emp):
            continue
        due = due_at(st["anchor"], start=emp["start_date"], probation_end=emp.get("probation_end"),
                     tz=emp["timezone"], default_time=s.default_send_time)
        late = due < now - timedelta(hours=s.stale_hours) and not _has_survey(st)
        r = conn.execute(
            """INSERT INTO events(employee_id, step_key, scenario_code, title, source_rows, due_at, status,
                                  needs_permission, skip_reason)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (employee_id, step_key) DO NOTHING RETURNING id""",
            (emp["id"], st["key"], sc["code"], st["title"], st["rows"], due,
             "skipped" if late else "scheduled", bool(st.get("needs_permission")),
             "late_start" if late else None)).fetchone()
        if r:
            created += 1
            skipped += int(late)
    return {"created": created, "skipped_past": skipped}


def reschedule_future(conn, emp: dict) -> int:
    """Start date / probation / timezone changed: move not-yet-started events."""
    s = get_settings()
    n = 0
    for ev in conn.execute("SELECT id, step_key FROM events WHERE employee_id=%s AND status='scheduled' AND NOT is_test",
                           (emp["id"],)).fetchall():
        st = step_by_key(ev["step_key"])
        if not st or "anchor" not in st:
            continue
        due = due_at(st["anchor"], start=emp["start_date"], probation_end=emp.get("probation_end"),
                     tz=emp["timezone"], default_time=s.default_send_time)
        conn.execute("UPDATE events SET due_at=%s, updated_at=now() WHERE id=%s", (due, ev["id"]))
        n += 1
    materialize_events(conn, emp)  # steps that became applicable (e.g. new tag)
    return n


def _has_survey(st: dict) -> bool:
    return any(a["type"] == "survey" for a in st["actions"])


def activation_problems(emp: dict) -> list[str]:
    missing = []
    if not (emp.get("short_name") or emp.get("full_name")):
        missing.append("имя")
    if not emp.get("organization"):
        missing.append("организация")
    if not emp.get("start_date"):
        missing.append("дата выхода")
    if not emp.get("timezone"):
        missing.append("часовой пояс")
    return missing


def effective_probation_end(emp: dict) -> date | None:
    if emp.get("probation_end"):
        return emp["probation_end"]
    return default_probation_end(emp["start_date"]) if emp.get("start_date") else None


# ---------------------------------------------------------------- materials
def find_material(conn, emp: dict, key: str) -> dict | None:
    m = conn.execute("SELECT * FROM materials WHERE key=%s AND employee_id=%s AND active ORDER BY id DESC LIMIT 1",
                     (key, emp["id"])).fetchone()
    if m:
        return m
    rows = conn.execute("SELECT * FROM materials WHERE key=%s AND employee_id IS NULL AND active ORDER BY id DESC",
                        (key,)).fetchall()
    tags = set(emp.get("tags") or [])
    for m in rows:  # tag-specific first, then common
        if m["tags"] and tags & set(m["tags"]):
            return m
    for m in rows:
        if not m["tags"]:
            return m
    return None


# ---------------------------------------------------------------- interpreter
class Ctx:
    def __init__(self, conn, emp: dict, event: dict, request_id: str | None = None, user_initiated=False):
        self.conn, self.emp, self.event, self.request_id = conn, emp, event, request_id
        self.state = dict(event.get("state") or {})
        self.warnings: list[str] = list(self.state.get("warnings", []))
        self.user_initiated = user_initiated

    def say(self, text: str, text_vy: str | None = None, *, buttons=None, materials=None, source="scenario"):
        return queue_to_employee(self.conn, self.emp, personalize(self.emp, text, text_vy),
                                 author_kind="bot_script" if source == "scenario" else "bot_system",
                                 source=source, buttons=buttons, material_ids=materials, request_id=self.request_id)


def run_actions(ctx: Ctx, actions: list[dict], start: int = 0, path: str = "") -> str:
    """Returns 'done' or 'waiting'."""
    today = local_today(ctx.emp)
    for i in range(start, len(actions)):
        a = actions[i]
        if not applies(a, ctx.emp, now_local_date=today):
            continue
        t = a["type"]
        if t == "text":
            mats = []
            if a.get("image"):
                m = find_material(ctx.conn, ctx.emp, a["image"])
                if m:
                    mats.append(m["id"])
                else:
                    ctx.warnings.append(f"Нет материала «{a['image']}» (строка {a.get('row')})")
            text = a["text"]
            if a.get("alt_if_it") and ctx.emp.get("it_access_confirmed"):
                text = a["alt_if_it"]
            ctx.say(text, a.get("text_vy"), materials=mats)
        elif t == "material":
            send_material(ctx, a)
        elif t == "set_address":
            ctx.conn.execute("UPDATE employees SET address_form=%s, updated_at=now() WHERE id=%s",
                             (a["value"], ctx.emp["id"]))
            ctx.emp["address_form"] = a["value"]
        elif t == "ask":
            text = a["text"]
            alt = (ctx.event.get("_step") or {}).get("alt_text_if_it_confirmed")
            if alt and ctx.emp.get("it_access_confirmed") and a["id"] == "access":
                text = alt
            vy = a.get("text_vy") if text == a["text"] else None
            buttons = [[{"text": personalize(ctx.emp, o["label"], o.get("label_vy")),
                         "payload": f"a:{ctx.event['id']}:{a['id']}~{i}~{path}:{o['id']}"}] for o in a["options"]]
            ctx.say(text, vy, buttons=buttons)
            ctx.state.update({"waiting": a["id"], "pc": i, "path": path})
            return "waiting"
        elif t == "survey":
            from .survey import start_survey
            key = a.get("survey")
            if not key:
                tags = ctx.emp.get("tags") or []
                by = a["survey_by_tag"]
                key = next((by[t_] for t_ in tags if t_ in by), by.get(""))
            if a.get("test"):
                key = f"{key}#test{ctx.event['id']}"
            start_survey(ctx.conn, ctx.emp, key, ctx.event["id"], request_id=ctx.request_id)
            ctx.state.update({"waiting": "survey", "pc": i, "path": path, "survey": key})
            return "waiting"
        elif t == "await_intro":
            ctx.conn.execute("UPDATE employees SET pending_input=%s WHERE id=%s",
                             (J({"type": "intro", "event_id": ctx.event["id"], "at": datetime.now(timezone.utc).isoformat()}), ctx.emp["id"]))
            ctx.say("Можно прислать текст и фото отдельными сообщениями. Если удобнее позже — просто нажми кнопку.",
                    "Можно прислать текст и фото отдельными сообщениями. Если удобнее позже — просто нажмите кнопку.",
                    buttons=[[{"text": "Пришлю позже", "payload": f"ix:{ctx.event['id']}"}]])
        elif t == "await_comment":
            ctx.conn.execute("UPDATE employees SET pending_input=%s WHERE id=%s",
                             (J({"type": "comment", "event_id": ctx.event["id"], "escalate": a.get("escalate"),
                                 "topic": a.get("topic"), "question": a.get("question"), "optional": True,
                                 "at": datetime.now(timezone.utc).isoformat()}),
                              ctx.emp["id"]))
        elif t == "case":
            from ..cases import open_case
            open_case(ctx.conn, ctx.emp, reason=a["reason"], priority=a["priority"], topic=a.get("topic", "other"),
                      question=a["question"], dedupe_key=f"event:{ctx.event['id']}:{a.get('topic')}",
                      context={"event": ctx.event["step_key"], "rows": ctx.event.get("source_rows")})
    return "done"


def send_material(ctx: Ctx, a: dict) -> None:
    m = find_material(ctx.conn, ctx.emp, a["key"])
    pre = personalize(ctx.emp, a.get("text") or "", a.get("text_vy"))
    if not m:
        ctx.warnings.append(f"Нет материала «{a['key']}» (строка {a.get('row')})")
        if ctx.user_initiated:
            from ..cases import open_case
            ctx.say("Сейчас у меня нет этого материала под рукой. Уточню у коллег из HR-службы и вернусь с ним сюда.",
                    "Сейчас у меня нет этого материала под рукой. Уточню у коллег из HR-службы и вернусь с ним сюда.",
                    source="system")
            open_case(ctx.conn, ctx.emp, reason="material_missing", priority="low", topic="other",
                      question=f"Сотрудник запросил материал «{a['key']}», но он не загружен в кабинет.",
                      dedupe_key=f"material:{ctx.emp['id']}:{a['key']}", set_waiting=False)
        elif pre:
            ctx.say(a.get("text") or "", a.get("text_vy"))
        return
    if m["kind"] == "link":
        ctx.say(((pre + "\n") if pre else "") + f"{m['title']}: {m['url']}")
    elif m["kind"] == "text":
        ctx.say(((pre + "\n\n") if pre else "") + (m["body"] or ""))
    else:
        ctx.say(pre or m["title"], materials=[m["id"]])


def _resolve_path(actions: list[dict], path: str) -> list[dict]:
    """path like '3.yes' -> actions[3].options[yes].then"""
    if not path:
        return actions
    cur = actions
    for part in path.split("/"):
        idx, opt = part.split(".")
        ask = cur[int(idx)]
        cur = next(o for o in ask["options"] if o["id"] == opt)["then"]
    return cur


def execute_event(conn, emp: dict, event: dict, request_id: str | None = None) -> str:
    st = step_by_key(event["step_key"])
    if st is None:
        conn.execute("UPDATE events SET status='failed', last_error='unknown_step' WHERE id=%s", (event["id"],))
        return "failed"
    event["_step"] = st
    ctx = Ctx(conn, emp, event, request_id)
    res = run_actions(ctx, st["actions"])
    _save(ctx, "waiting_reply" if res == "waiting" else "sent")
    return res


def _save(ctx: Ctx, status: str) -> None:
    ctx.state["warnings"] = ctx.warnings
    ctx.conn.execute("""UPDATE events SET status=%s, state=%s, sent_at=COALESCE(sent_at, now()), updated_at=now(),
                        last_error=%s WHERE id=%s""",
                     (status, J(ctx.state), "; ".join(ctx.warnings) or None, ctx.event["id"]))


def on_ask_reply(conn, emp: dict, event_id: int, ask_id: str, opt_id: str, request_id=None, message_id=None) -> str:
    """Button press (or unambiguous text) for a scenario ask. Returns ok|stale.
    Button payloads carry ask id + position, so an old button never answers another ask of the same step."""
    ev = conn.execute("SELECT * FROM events WHERE id=%s AND employee_id=%s FOR UPDATE", (event_id, emp["id"])).fetchone()
    pos = None
    if "~" in ask_id:
        ask_id, pc_s, pth = ask_id.split("~", 2)
        pos = (int(pc_s), pth)
    st_ = (ev or {}).get("state") or {}
    if not ev or ev["status"] != "waiting_reply" or st_.get("waiting") != ask_id:
        return "stale"
    if pos is not None and (st_.get("pc"), st_.get("path", "")) != pos:
        return "stale"
    st = step_by_key(ev["step_key"])
    ev["_step"] = st
    ctx = Ctx(conn, emp, ev, request_id, user_initiated=True)
    pc, path = ctx.state["pc"], ctx.state.get("path", "")
    level = _resolve_path(st["actions"], path)
    ask = level[pc]
    option = next((o for o in ask["options"] if o["id"] == opt_id), None)
    if option is None:
        return "stale"
    answers = ctx.state.setdefault("answers", {})
    answers[(path + "/" if path else "") + ask_id] = opt_id
    ctx.state.pop("waiting", None)
    if message_id is None:  # typed answers are already stored by the inbound handler
        ctx.conn.execute("INSERT INTO messages(employee_id, direction, author_kind, text, payload) VALUES (%s,'in','employee',%s,%s)",
                         (emp["id"], option["label"], J({"ask": ask_id, "option": opt_id, "event_id": event_id})))
    sub_path = (path + "/" if path else "") + f"{pc}.{opt_id}"
    res = run_actions(ctx, option["then"], 0, sub_path)
    if res == "done":
        # continue after the ask on the same level, then unwind to parents
        res = run_actions(ctx, level, pc + 1, path)
        while res == "done" and path:
            parent_path, _, last = path.rpartition("/")
            idx = int(last.split(".")[0])
            res = run_actions(ctx, _resolve_path(st["actions"], parent_path), idx + 1, parent_path)
            path = parent_path
    _save(ctx, "waiting_reply" if res == "waiting" else "done")
    return "ok"


def on_survey_finished(conn, emp: dict, event_id: int | None) -> None:
    if not event_id:
        return
    ev = conn.execute("SELECT * FROM events WHERE id=%s FOR UPDATE", (event_id,)).fetchone()
    if not ev or (ev["state"] or {}).get("waiting") != "survey":
        return
    st = step_by_key(ev["step_key"])
    ev["_step"] = st
    ctx = Ctx(conn, emp, ev)
    ctx.state.pop("waiting", None)
    res = run_actions(ctx, st["actions"], ctx.state.get("pc", 0) + 1)
    _save(ctx, "waiting_reply" if res == "waiting" else "done")


def pending_asks(conn, emp_id: int) -> list[dict]:
    """Open button questions of the employee: [{event_id, ask_id, options:[{id,label}], text}]"""
    out = []
    # text answers only bind to recent questions (24h); older ones still work via their buttons
    for ev in conn.execute("""SELECT * FROM events WHERE employee_id=%s AND status='waiting_reply'
                              AND state->>'waiting' IS NOT NULL AND state->>'waiting' <> 'survey'
                              AND updated_at > now() - interval '24 hours'""",
                           (emp_id,)).fetchall():
        st = step_by_key(ev["step_key"])
        level = _resolve_path(st["actions"], ev["state"].get("path", ""))
        ask = level[ev["state"]["pc"]]
        out.append({"event_id": ev["id"], "ask_id": ask["id"], "text": ask["text"], "options": ask["options"]})
    return out


def resend_open_asks(conn, emp: dict) -> int:
    """After an HR conversation: button questions whose message was cancelled/undelivered come back once."""
    n = 0
    for ev in conn.execute("""SELECT * FROM events WHERE employee_id=%s AND status='waiting_reply'
                              AND state->>'waiting' IS NOT NULL AND state->>'waiting' <> 'survey'""", (emp["id"],)).fetchall():
        prefix = f"a:{ev['id']}:"
        delivered = conn.execute("""SELECT 1 FROM outbox WHERE employee_id=%s AND status IN ('sent','pending','sending')
                                    AND body->'buttons'->0->0->>'payload' LIKE %s LIMIT 1""",
                                 (emp["id"], prefix + "%")).fetchone()
        if delivered:
            continue
        st = step_by_key(ev["step_key"])
        state = ev["state"]
        ask = _resolve_path(st["actions"], state.get("path", ""))[state["pc"]]
        ev["_step"] = st
        ctx = Ctx(conn, emp, ev)
        buttons = [[{"text": personalize(emp, o["label"], o.get("label_vy")),
                     "payload": f"a:{ev['id']}:{ask['id']}~{state['pc']}~{state.get('path', '')}:{o['id']}"}] for o in ask["options"]]
        ctx.say(ask["text"], ask.get("text_vy"), buttons=buttons)
        conn.execute("UPDATE events SET updated_at=now() WHERE id=%s", (ev["id"],))
        n += 1
    return n
