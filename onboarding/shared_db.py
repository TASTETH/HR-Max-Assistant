"""Единственный SQL-адаптер общей схемы korsik (PostgreSQL 17+).

Транзакции предоставляет вызывающий backend. Модуль не создаёт таблиц и не
меняет схему; использует операции из согласованного архива БД.
"""
from sqlalchemy import text


class RejectedBeforeAcceptance(Exception):
    """Транспорт ТОЧНО знает, что MAX не принял сообщение; повтор допустим."""


def check_database(session):
    """Разовая проверка целевой версии и публичного контракта, без DDL."""
    version = int(session.scalar(text("SELECT current_setting('server_version_num')")))
    if version < 170000:
        raise ValueError("Общая схема требует PostgreSQL 17 или новее")
    signatures = (
        "korsik.prepare_due_events(integer)", "korsik.claim_outbox(integer)",
        "korsik.finish_send(bigint,bigint,text,text,boolean)", "korsik.recover_leases()",
        "korsik.assign_plan(bigint,bigint,bigint)", "korsik.answer_event(bigint,bigint,text,text)",
    )
    missing = [sig for sig in signatures if session.scalar(
        text("SELECT to_regprocedure(:signature)::text"), {"signature": sig}) is None]
    if missing:
        raise ValueError("Не установлены функции общей БД: " + ", ".join(missing))
    return {"server_version_num": version, "schema": "korsik", "contract": "archive-v1"}


def _id(value, name):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ValueError(name + " должен быть положительным BIGINT из общей БД")
    return value


def resolve_employee_id(session, external_employee_id):
    """Старый строковый ID — external_employee_id, не принудительное преобразование в int."""
    value = session.scalar(text("SELECT id FROM korsik.employees WHERE external_employee_id=:external"),
                           {"external": external_employee_id})
    if value is None:
        raise ValueError("Сотрудник с таким external_employee_id не найден")
    return value


def assign_plan(session, hr_id, employee_id, version_id):
    """Даты и HR проверяет функция общей БД; никаких параллельных onboarding_* записей."""
    return session.scalar(text("SELECT korsik.assign_plan(:hr,:employee,:version)"), {
        "hr": _id(hr_id, "hr_id"), "employee": _id(employee_id, "employee_id"),
        "version": _id(version_id, "version_id")})


def answer_simple_event(session, employee_id, event_id, answer_type, value):
    """Вызывать внутри общей обработки max_inbox, после проверки контекста ответа.

    Сохранение входящего события и результата выполняет общий backend в одной
    транзакции. Обычный вопрос во время опроса сюда не направляется.
    """
    if answer_type not in ("rating", "comment", "task_confirm", "feedback"):
        raise ValueError("Общая схема пока не поддерживает этот тип ответа")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Нужен непустой текст ответа")
    return session.scalar(text("SELECT korsik.answer_event(:employee,:event,:kind,:value)"), {
        "employee": _id(employee_id, "employee_id"), "event": _id(event_id, "event_id"),
        "kind": answer_type, "value": value})


def claim_message(session):
    # По одному: не расходуем двухминутную аренду последних задач большой пачки.
    row = session.execute(text("SELECT * FROM korsik.claim_outbox(1)")).mappings().first()
    if row is None:
        return None
    details = session.execute(text("""
        SELECT m.employee_id,m.event_id,m.deduplication_key,o.purpose
        FROM korsik.messages m JOIN korsik.outbox o ON o.message_id=m.id
        WHERE m.id=:id
    """), {"id": row["message_id"]}).mappings().one()
    return {"message_id": row["message_id"], "generation": row["generation"],
            "employee_id": details["employee_id"], "step_id": details["event_id"],
            "chat_id": row["max_chat_id"], "text": row["body"],
            "idempotency_key": details["deduplication_key"], "purpose": details["purpose"]}


def finish_send(session, message, *, external_id=None, error=None, uncertain=False):
    session.execute(text("SELECT korsik.finish_send(:id,:generation,:external,:error,:uncertain)"), {
        "id": message["message_id"], "generation": message["generation"], "external": external_id,
        "error": error, "uncertain": uncertain})


def get_progress(session, employee_id):
    employee_id = _id(employee_id, "employee_id")
    plan = session.execute(text("""
        SELECT id,version_id,status,start_date,timezone FROM korsik.employee_plans
        WHERE employee_id=:employee ORDER BY (status='active') DESC,assigned_at DESC,id DESC LIMIT 1
    """), {"employee": employee_id}).mappings().first()
    if plan is None:
        return {"employee_id": employee_id, "plan": None, "events": []}
    events = session.execute(text("""
        SELECT id,code,position,kind,status,scheduled_at,deadline_at,required,
               rating,comment,answered_at,completed_at
        FROM korsik.plan_events WHERE plan_id=:plan ORDER BY position
    """), {"plan": plan["id"]}).mappings().all()
    return {"employee_id": employee_id, "plan": dict(plan), "events": [dict(event) for event in events]}


def recover_leases(session):
    return dict(session.execute(text("SELECT * FROM korsik.recover_leases()")).mappings().one())


def prepare_due_events(session, batch_size):
    return session.scalar(text("SELECT korsik.prepare_due_events(:batch)"), {"batch": batch_size})


def unresolved_delivery(session):
    """Временный общий стоп до исправления сериализации claim_outbox в БД."""
    return session.scalar(text("""
        SELECT message_id FROM korsik.outbox WHERE status IN ('sending','uncertain')
        ORDER BY message_id LIMIT 1
    """))


def delivery_is_current(session, message_id, generation):
    return bool(session.scalar(text("""
        SELECT status='sending' AND generation=:generation AND lease_until>clock_timestamp()
        FROM korsik.outbox WHERE message_id=:id
    """), {"id": message_id, "generation": generation}))


# Совместимость прежних импортов; рабочая реализация только в scheduler.py.
def run_scheduler_tick(session_factory, *, batch_size=100):
    from .scheduler import run_scheduler_tick as run
    return run(session_factory, batch_size=batch_size)


def run_delivery_tick(session_factory, send_message, *, batch_size=50):
    from .scheduler import run_delivery_tick as run
    return run(session_factory, send_message, batch_size=batch_size)

