"""Два рабочих прохода: подготовить события и отправить готовую очередь.

Периодический вызов организует Celery Beat через tasks.py. Сроки, состояния,
повторные попытки и аренды находятся в общей PostgreSQL, а не в памяти процесса.
"""
import logging

from . import shared_db

LOGGER = logging.getLogger(__name__)
RejectedBeforeAcceptance = shared_db.RejectedBeforeAcceptance


def _batch_size(value):
    if type(value) is not int or not 1 <= value <= 1000:
        raise ValueError("batch_size должен быть от 1 до 1000")
    return value


def run_scheduler_tick(session_factory, *, batch_size=100):
    """Один короткий проход: восстановить аренды, подготовить события/напоминания."""
    _batch_size(batch_size)
    with session_factory.begin() as session:
        recovered = shared_db.recover_leases(session)
        count = shared_db.prepare_due_events(session, batch_size)
    LOGGER.info("Onboarding schedule: queued=%s recovered=%s", count, recovered)
    return {"queued_messages": count, **recovered}


def run_delivery_tick(session_factory, send_message, *, batch_size=50):
    """Отправляет сообщения по одному, закрывая SQL-транзакции перед сетью.

    send_message(dict) — синхронный MAX-адаптер команды. Возвращает внешний ID.
    Явный отказ до принятия — RejectedBeforeAcceptance; неизвестный исход — uncertain.
    До исправления claim_outbox: только один consumer общей очереди отправки.
    """
    _batch_size(batch_size)
    if not callable(send_message):
        raise TypeError("Нужна функция отправки сообщения в MAX")
    accepted, rejected, uncertain, stale, blocked = [], [], [], [], []
    for _ in range(batch_size):
        with session_factory.begin() as session:
            unresolved = shared_db.unresolved_delivery(session)
            if unresolved is not None:
                blocked.append(unresolved)
                message = None
            else:
                message = shared_db.claim_message(session)
        if blocked:
            LOGGER.warning("Delivery paused: unresolved outbox message %s", blocked[0])
            break
        if message is None:
            break
        with session_factory.begin() as session:
            valid = shared_db.delivery_is_current(session, message["message_id"], message["generation"])
        if not valid:
            stale.append(message["message_id"])
            continue
        try:
            external_id = send_message(message)  # Сеть: открытой транзакции нет.
            if not isinstance(external_id, str) or not external_id.strip():
                raise TypeError("MAX-адаптер должен вернуть ID принятого сообщения")
        except RejectedBeforeAcceptance:
            with session_factory.begin() as session:
                shared_db.finish_send(session, message, error="MAX_REJECTED", uncertain=False)
            rejected.append(message["message_id"])
        except Exception:
            with session_factory.begin() as session:
                shared_db.finish_send(session, message, error="MAX_RESULT_UNKNOWN", uncertain=True)
            uncertain.append(message["message_id"])
            break
        else:
            with session_factory.begin() as session:
                shared_db.finish_send(session, message, external_id=external_id)
            accepted.append(message["message_id"])
    result = {"accepted": accepted, "rejected": rejected, "uncertain": uncertain,
              "stale": stale, "blocked_by_unresolved_delivery": blocked}
    LOGGER.info("Onboarding delivery: accepted=%s rejected=%s uncertain=%s stale=%s blocked=%s",
                len(accepted), len(rejected), len(uncertain), len(stale), blocked)
    return result
