"""Numbers for HR analytics, manager views and the weekly digest — computed by code from the DB.
Messages, answers and employees are counted separately; reprocessing does not double count
(one analysis per source and analyzer version); 'unknown' stays unknown."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .analysis import VERSION
from .cases import TOPIC_LABELS, notify
from .config import get_settings
from .db import J, tx
from .scenario.engine import effective_probation_end

SURVEY_STEPS = ("d3_survey", "d14_check", "m1w3_survey", "final_survey")


def stage_of(emp: dict, today: date) -> str:
    if emp["status"] == "completed":
        return "Завершил адаптацию"
    if not emp.get("start_date"):
        return "Черновик / без даты"
    d = (today - emp["start_date"]).days
    if d < 0:
        return "Преонбординг"
    if d < 7:
        return "Первая неделя"
    if d < 31:
        return "Первый месяц"
    if d < 62:
        return "Второй месяц"
    return "Третий месяц"


def progress(c, emp_id: int) -> dict:
    ev = c.execute("""SELECT step_key, status FROM events WHERE employee_id=%s AND NOT is_test""", (emp_id,)).fetchall()
    total = [e for e in ev if e["status"] not in ("skipped", "cancelled")]
    done = [e for e in total if e["status"] in ("sent", "done")]
    waiting = [e for e in total if e["status"] == "waiting_reply"]
    runs = c.execute("SELECT status, reminders_sent FROM survey_runs WHERE employee_id=%s AND survey_key NOT LIKE '%%#test%%'",
                     (emp_id,)).fetchall()
    return {"events_total": len(total), "events_done": len(done), "waiting_reply": len(waiting),
            "surveys_completed": sum(r["status"] == "completed" for r in runs),
            "surveys_open": sum(r["status"] in ("active", "queued") for r in runs),
            "surveys_overdue": sum(r["status"] == "active" and r["reminders_sent"] >= 2 for r in runs),
            "skipped": sum(e["status"] == "skipped" for e in ev)}


def compute(c, start: datetime, end: datetime, *, employee_ids: list[int] | None = None) -> dict:
    flt = "AND employee_id = ANY(%(ids)s)" if employee_ids is not None else ""
    p = {"s": start, "e": end, "ids": employee_ids, "v": VERSION}
    emps = c.execute("SELECT * FROM employees WHERE status <> 'deactivated'" +
                     (" AND id = ANY(%(ids)s)" if employee_ids is not None else ""), p).fetchall()
    today = datetime.now(timezone.utc).date()
    stages: dict[str, int] = {}
    for e in emps:
        stages[stage_of(e, today)] = stages.get(stage_of(e, today), 0) + 1
    sent = c.execute(f"""SELECT sentiment, count(*) n, count(DISTINCT employee_id) emps FROM analyses
                         WHERE analyzer_version=%(v)s AND created_at >= %(s)s AND created_at < %(e)s {flt}
                         GROUP BY sentiment""", p).fetchall()
    topics = c.execute(f"""SELECT t AS topic, count(*) mentions, count(DISTINCT employee_id) emps FROM analyses,
                           unnest(topics) t WHERE analyzer_version=%(v)s AND created_at >= %(s)s AND created_at < %(e)s
                           AND (priority IN ('medium','high') OR sentiment='negative') {flt}
                           GROUP BY t ORDER BY 3 DESC, 2 DESC""", p).fetchall()
    answers = c.execute(f"""SELECT count(*) n, count(DISTINCT employee_id) emps FROM survey_answers
                            WHERE updated_at >= %(s)s AND updated_at < %(e)s {flt}""", p).fetchone()
    msgs = c.execute(f"""SELECT count(*) n, count(DISTINCT employee_id) emps FROM messages
                         WHERE direction='in' AND author_kind='employee' AND created_at >= %(s)s AND created_at < %(e)s {flt}""",
                     p).fetchone()
    runs = c.execute(f"""SELECT
                           count(*) FILTER (WHERE status='completed' AND completed_at >= %(s)s AND completed_at < %(e)s) completed,
                           count(*) FILTER (WHERE status='active') open,
                           count(*) FILTER (WHERE status='active' AND reminders_sent >= 2) overdue
                         FROM survey_runs WHERE survey_key NOT LIKE '%%#test%%' {flt}""", p).fetchone()
    cases = c.execute(f"""SELECT status, priority, count(*) n FROM cases WHERE true {flt} GROUP BY status, priority""", p).fetchall()
    signals = c.execute(f"""SELECT a.priority, a.summary, a.topics, e.full_name, e.department, a.created_at
                            FROM analyses a JOIN employees e ON e.id=a.employee_id
                            WHERE a.analyzer_version=%(v)s AND a.priority='high' AND a.created_at >= %(s)s
                              AND a.created_at < %(e)s {flt.replace('employee_id', 'a.employee_id')}
                            ORDER BY a.created_at DESC LIMIT 20""", p).fetchall()
    respondents = c.execute(f"""SELECT count(DISTINCT employee_id) n FROM (
                                  SELECT employee_id FROM survey_answers WHERE updated_at >= %(s)s AND updated_at < %(e)s {flt}
                                  UNION SELECT employee_id FROM analyses WHERE analyzer_version=%(v)s
                                    AND source_type='message' AND created_at >= %(s)s AND created_at < %(e)s {flt}) x""",
                            p).fetchone()["n"]
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "coverage": {"employees": len(emps), "in_adaptation": sum(e["status"] == "active" for e in emps),
                     "linked_max": sum(bool(e["max_user_id"]) for e in emps), "respondents": respondents},
        "stages": stages,
        "sentiment": {r["sentiment"]: {"items": r["n"], "employees": r["emps"]} for r in sent},
        "topics": [{"topic": r["topic"], "label": TOPIC_LABELS.get(r["topic"], r["topic"]), "mentions": r["mentions"],
                    "employees": r["emps"]} for r in topics],
        "survey_answers": {"answers": answers["n"], "employees": answers["emps"]},
        "messages": {"messages": msgs["n"], "employees": msgs["emps"]},
        "surveys": dict(runs),
        "cases": [dict(r) for r in cases],
        "high_signals": [{"summary": r["summary"], "topics": r["topics"], "employee": r["full_name"],
                          "department": r["department"], "at": r["created_at"].isoformat()} for r in signals],
    }


def weekly(c, user: dict, *, now: datetime | None = None) -> dict | None:
    s = get_settings()
    tz = ZoneInfo(s.default_timezone)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    monday = now.date() - timedelta(days=now.weekday())
    start = datetime.combine(monday - timedelta(days=7), time(0), tzinfo=tz)
    end = datetime.combine(monday, time(0), tzinfo=tz)
    exists = c.execute("SELECT id FROM digests WHERE period_start=%s AND recipient_user_id=%s",
                       (start.date(), user["id"])).fetchone()
    if exists:
        return None
    data = compute(c, start, end)
    prev = c.execute("""SELECT data FROM digests WHERE recipient_user_id=%s AND period_start < %s
                        ORDER BY period_start DESC LIMIT 1""", (user["id"], start.date())).fetchone()
    data["previous"] = prev["data"] if prev else None
    text = render_digest(data)
    row = c.execute("""INSERT INTO digests(period_start, period_end, recipient_user_id, data, text)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
                    (start.date(), (end - timedelta(days=1)).date(), user["id"], J(data), text)).fetchone()
    if row:
        notify(c, user, kind="digest", title=f"Дайджест «Настроения новичков» {start:%d.%m}–{(end - timedelta(days=1)):%d.%m}",
               body=text[:3500], dedupe_key=f"digest:{start.date()}:{user['id']}")
    return data


def _pct(part, whole):
    return f"{round(100 * part / whole)}%" if whole else "—"


def render_digest(d: dict) -> str:
    cov = d["coverage"]
    sent = d["sentiment"]
    total_items = sum(v["items"] for v in sent.values())
    lines = [f"Охват: {cov['in_adaptation']} сотрудников в адаптации, ответили/писали за неделю: {cov['respondents']}."]
    if not total_items:
        lines.append("За период нет проанализированных ответов — оценка настроений не строится.")
    else:
        lab = {"positive": "позитив", "neutral": "нейтрально", "negative": "негатив", "unknown": "не определено"}
        lines.append("Тональность: " + ", ".join(f"{lab[k]} {v['items']} ({_pct(v['items'], total_items)})"
                                                for k, v in sent.items()) + ".")
    if d["topics"]:
        lines.append("Темы трудностей: " + "; ".join(f"{t['label']} — {t['employees']} чел." for t in d["topics"][:6]) + ".")
    open_cases = sum(r["n"] for r in d["cases"] if r["status"] != "resolved")
    high = sum(r["n"] for r in d["cases"] if r["status"] != "resolved" and r["priority"] == "high")
    lines.append(f"Открытые обращения: {open_cases}, из них высокого приоритета: {high}.")
    lines.append(f"Опросы: завершено за неделю {d['surveys']['completed']}, ждут ответа {d['surveys']['open']}, "
                 f"без ответа после 2 напоминаний {d['surveys']['overdue']}.")
    if d["high_signals"]:
        lines.append("Зоны риска: " + "; ".join(f"{s['employee']} ({s['department'] or '—'}): {s['summary'][:80]}"
                                               for s in d["high_signals"][:5]) + ".")
    prev = d.get("previous")
    if prev:
        pn = prev["sentiment"].get("negative", {}).get("items", 0)
        cn = sent.get("negative", {}).get("items", 0)
        lines.append(f"Динамика: негативных сигналов {cn} (неделей ранее {pn}); ответивших {cov['respondents']} "
                     f"(неделей ранее {prev['coverage']['respondents']}).")
    else:
        lines.append("Динамика: первый дайджест — сравнения с прошлым периодом пока нет.")
    return "\n".join(lines)


def probation_meetings(c) -> int:
    """Rule: 7 days before the end of probation the manager gets a meeting recommendation."""
    from .cases import manager_meeting
    n = 0
    for e in c.execute("SELECT * FROM employees WHERE status='active' AND manager_user_id IS NOT NULL AND start_date IS NOT NULL").fetchall():
        end = effective_probation_end(e)
        today = datetime.now(ZoneInfo(e["timezone"])).date()
        if end and today >= end - timedelta(days=7) and today <= end:
            manager_meeting(c, e, basis=f"через неделю завершается испытательный срок ({end:%d.%m.%Y}) — нужна встреча по итогам",
                            dedupe=f"meeting:probation:{e['id']}")
            n += 1
    return n
