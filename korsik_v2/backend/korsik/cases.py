"""Internal questions (cases), seamless HR takeover of the same MAX dialog, notifications.

Dialog modes per employee:
  auto        — scenario, reference answers and analysis work normally;
  waiting_hr  — a case is registered and HR notified; additions are attached to it;
  hr_active   — the single HR owns the dialog: no automatic answers/surveys/broadcasts.
Case state (new/in_progress/resolved) is separate from the dialog mode.
"""
from __future__ import annotations

import logging

from .db import J, audit
from .outbound import personalize, queue_to_employee, queue_to_user

log = logging.getLogger(__name__)

TOPIC_LABELS = {
    "access": "доступы/оборудование", "tasks": "задачи", "workload": "нагрузка",
    "colleagues": "коллеги/наставник", "manager": "руководитель", "hr_admin": "кадровые вопросы",
    "retention": "намерение уйти", "other": "другое",
}
REASON_LABELS = {
    "no_answer": "нет подтвержденного ответа в материалах", "personal_data": "персональные сведения",
    "feedback_signal": "сигнал в обратной связи", "human_request": "просьба о личном общении",
    "service_error": "сбой автоматической обработки", "material_missing": "нет материала",
}
PRIO_ORDER = {"low": 0, "medium": 1, "high": 2}


def single_hr(conn) -> dict:
    from .config import get_settings
    hr = conn.execute("SELECT * FROM users WHERE login=%s AND role='hr' AND active", (get_settings().hr_login,)).fetchone()
    if not hr:
        hr = conn.execute("SELECT * FROM users WHERE role='hr' AND active ORDER BY id LIMIT 1").fetchone()
    if not hr:
        raise RuntimeError("HR не создан: выполните команду create-hr")
    return hr


def open_case(conn, emp: dict, *, reason: str, priority: str, topic: str, question: str,
              root_message_id: int | None = None, dedupe_key: str | None = None,
              context: dict | None = None, needs_review: bool = False, set_waiting: bool = True) -> dict:
    """Register a case for the single HR (idempotent by dedupe_key) and queue the HR notification.
    Does not depend on RAG/LLM/delivery state."""
    hr = single_hr(conn)
    row = conn.execute(
        """INSERT INTO cases(employee_id, hr_user_id, root_message_id, reason, priority, needs_review, topic,
                             question, context, dedupe_key)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (dedupe_key) DO NOTHING RETURNING *""",
        (emp["id"], hr["id"], root_message_id, reason, priority, needs_review, topic, question,
         J(context or {}), dedupe_key)).fetchone()
    if not row:
        existing = conn.execute("SELECT * FROM cases WHERE dedupe_key=%s", (dedupe_key,)).fetchone()
        if existing and PRIO_ORDER[priority] > PRIO_ORDER[existing["priority"]] and existing["status"] != "resolved":
            conn.execute("UPDATE cases SET priority=%s WHERE id=%s", (priority, existing["id"]))
        return existing
    if root_message_id:
        conn.execute("UPDATE messages SET case_id=%s WHERE id=%s", (row["id"], root_message_id))
    if set_waiting:
        conn.execute("""UPDATE employees SET dialog_mode = CASE WHEN dialog_mode='auto' THEN 'waiting_hr' ELSE dialog_mode END,
                        active_case_id = COALESCE(active_case_id, %s), updated_at=now() WHERE id=%s""",
                     (row["id"], emp["id"]))
        emp["dialog_mode"] = "waiting_hr" if emp.get("dialog_mode") == "auto" else emp.get("dialog_mode")
    name = emp.get("full_name") or emp.get("short_name") or f"#{emp['id']}"
    prio = {"low": "низкий", "medium": "средний", "high": "ВЫСОКИЙ"}[priority]
    notify(conn, hr, kind="new_case", employee_id=emp["id"], case_id=row["id"],
           title=f"Новое обращение: {name}",
           body=f"Приоритет: {prio}. Причина: {REASON_LABELS[reason]}. Тема: {TOPIC_LABELS.get(topic, topic)}.\n"
                f"«{question[:300]}»",
           dedupe_key=f"case:{row['id']}:new")
    # topic 'manager' is deliberately excluded: a manager must not learn that an employee complained about them
    if reason == "feedback_signal" and priority in ("medium", "high") and topic in ("tasks", "colleagues", "retention", "workload"):
        manager_meeting(conn, emp, basis="по ответам в адаптационном опросе сотруднику может понадобиться поддержка",
                        dedupe=f"meeting:case:{row['id']}")
    return row


def notify(conn, user: dict, *, kind: str, title: str, body: str, dedupe_key: str,
           employee_id=None, case_id=None, reason: str = "") -> None:
    n = conn.execute(
        """INSERT INTO notifications(recipient_user_id, kind, employee_id, case_id, title, body, reason, dedupe_key)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (dedupe_key) DO NOTHING RETURNING id""",
        (user["id"], kind, employee_id, case_id, title, body, reason, dedupe_key)).fetchone()
    if not n:
        return
    ob = queue_to_user(conn, user, f"🔔 {title}\n{body}")
    conn.execute("UPDATE notifications SET channel_status=%s, outbox_id=%s WHERE id=%s",
                 ("queued" if ob else "no_channel", ob, n["id"]))


def manager_meeting(conn, emp: dict, *, basis: str, dedupe: str) -> None:
    """Proactive note to the employee's manager. Contains basis, not private details."""
    if not emp.get("manager_user_id"):
        return
    mgr = conn.execute("SELECT * FROM users WHERE id=%s AND active", (emp["manager_user_id"],)).fetchone()
    if not mgr:
        return
    name = emp.get("full_name") or emp.get("short_name")
    notify(conn, mgr, kind="manager_meeting", employee_id=emp["id"],
           title=f"Рекомендуется личная встреча: {name}",
           body=f"Основание: {basis}. Встреча не назначена автоматически — запланируйте её, пожалуйста.",
           reason=basis, dedupe_key=dedupe)


def attach_to_case(conn, emp: dict, message_id: int) -> None:
    if emp.get("active_case_id"):
        conn.execute("UPDATE messages SET case_id=%s WHERE id=%s", (emp["active_case_id"], message_id))
        hr = single_hr(conn)
        conn.execute("""INSERT INTO notifications(recipient_user_id, kind, employee_id, case_id, title, body, dedupe_key, channel_status)
                        VALUES (%s,'case_update',%s,%s,%s,%s,%s,'no_channel') ON CONFLICT DO NOTHING""",
                     (hr["id"], emp["id"], emp["active_case_id"], "Новое сообщение в обращении",
                      "Сотрудник дописал сообщение", f"case:{emp['active_case_id']}:msg:{message_id}"))


# ---------------------------------------------------------------- HR actions
def take(conn, hr_user: dict, case_id: int) -> dict:
    case = conn.execute("SELECT * FROM cases WHERE id=%s FOR UPDATE", (case_id,)).fetchone()
    if not case:
        raise LookupError("case")
    emp = conn.execute("SELECT * FROM employees WHERE id=%s FOR UPDATE", (case["employee_id"],)).fetchone()
    conn.execute("UPDATE cases SET status='in_progress', taken_at=COALESCE(taken_at, now()) WHERE id=%s", (case_id,))
    conn.execute("""UPDATE employees SET dialog_mode='hr_active', dialog_revision=dialog_revision+1,
                    active_case_id=%s, updated_at=now() WHERE id=%s""", (case_id, emp["id"]))
    # cancel automatic messages and answers that have not started yet
    conn.execute("""UPDATE outbox SET status='cancelled', last_error='hr_took_dialog'
                    WHERE employee_id=%s AND status='pending' AND requires_revision IS NOT NULL""", (emp["id"],))
    conn.execute("""UPDATE jobs SET status='done', last_error='superseded_by_hr', finished_at=now()
                    WHERE status='pending' AND kind='free_message' AND (payload->>'employee_id')::bigint=%s""",
                 (emp["id"],))
    audit(conn, hr_user["id"], "take_case", f"case:{case_id}")
    return case


def hr_reply(conn, hr_user: dict, employee_id: int, text: str) -> int:
    emp = conn.execute("SELECT * FROM employees WHERE id=%s", (employee_id,)).fetchone()
    if emp["dialog_mode"] != "hr_active":
        raise PermissionError("Сначала возьмите диалог в работу")
    if not emp["max_chat_id"]:
        raise PermissionError("Сотрудник ещё не привязан к MAX")
    ob = queue_to_employee(conn, emp, text.strip(), author_kind="hr", author_user_id=hr_user["id"],
                           source="human", case_id=emp["active_case_id"])
    audit(conn, hr_user["id"], "hr_reply", f"employee:{employee_id}", {"outbox": ob})
    return ob


def release(conn, hr_user: dict, employee_id: int, *, resolve_case: bool = True) -> str:
    from .scenario.engine import resend_open_asks
    from .scenario.survey import resend_current, shift_reminders_after_pause
    emp = conn.execute("SELECT * FROM employees WHERE id=%s FOR UPDATE", (employee_id,)).fetchone()
    if emp["dialog_mode"] != "hr_active":
        raise PermissionError("Диалог не в работе у HR")
    taken = conn.execute("SELECT taken_at FROM cases WHERE id=%s", (emp["active_case_id"],)).fetchone() if emp["active_case_id"] else None
    taken_at = (taken or {}).get("taken_at")
    if resolve_case and emp["active_case_id"]:
        conn.execute("UPDATE cases SET status='resolved', resolved_at=now(), resolved_by=%s WHERE id=%s",
                     (hr_user["id"], emp["active_case_id"]))
    other = conn.execute("""SELECT id FROM cases WHERE employee_id=%s AND status='new' ORDER BY
                            CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id LIMIT 1""",
                         (employee_id,)).fetchone()
    mode = "waiting_hr" if other else "auto"
    conn.execute("""UPDATE employees SET dialog_mode=%s, dialog_revision=dialog_revision+1, active_case_id=%s,
                    updated_at=now() WHERE id=%s""", (mode, other["id"] if other else None, employee_id))
    # catch-up rule: informational events that fell due during the HR conversation are skipped
    conn.execute("""UPDATE events e SET status='skipped', skip_reason='paused_during_hr', updated_at=now()
                    WHERE e.employee_id=%s AND e.status='scheduled' AND e.due_at < now()
                      AND e.due_at >= COALESCE(%s, now())
                      AND NOT (e.needs_permission AND e.permitted_at IS NULL)
                      AND e.step_key NOT IN ('d3_survey','d14_check','m1w3_survey','final_survey','d3_plan')
                      AND NOT e.is_test""", (employee_id, taken_at))
    emp = conn.execute("SELECT * FROM employees WHERE id=%s", (employee_id,)).fetchone()
    shift_reminders_after_pause(conn, emp)
    resend_current(conn, emp)   # the waiting survey question comes back once
    resend_open_asks(conn, emp)  # button questions cancelled during the conversation come back once
    audit(conn, hr_user["id"], "release_dialog", f"employee:{employee_id}", {"mode": mode})
    return mode


def resolve(conn, hr_user: dict, case_id: int) -> None:
    case = conn.execute("SELECT * FROM cases WHERE id=%s FOR UPDATE", (case_id,)).fetchone()
    conn.execute("UPDATE cases SET status='resolved', resolved_at=now(), resolved_by=%s WHERE id=%s",
                 (hr_user["id"], case_id))
    emp = conn.execute("SELECT * FROM employees WHERE id=%s FOR UPDATE", (case["employee_id"],)).fetchone()
    if emp["dialog_mode"] == "waiting_hr":
        other = conn.execute("SELECT id FROM cases WHERE employee_id=%s AND status='new' AND id<>%s LIMIT 1",
                             (emp["id"], case_id)).fetchone()
        conn.execute("UPDATE employees SET dialog_mode=%s, active_case_id=%s, updated_at=now() WHERE id=%s",
                     ("waiting_hr" if other else "auto", other["id"] if other else None, emp["id"]))
    elif emp["active_case_id"] == case_id and emp["dialog_mode"] != "hr_active":
        conn.execute("UPDATE employees SET active_case_id=NULL WHERE id=%s", (emp["id"],))
    audit(conn, hr_user["id"], "resolve_case", f"case:{case_id}")
