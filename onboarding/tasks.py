"""Регистрация задач в УЖЕ существующем Celery-приложении команды.

Этот файл не создаёт второе приложение, не запускает Redis и не открывает БД.
"""
def register_tasks(celery_app, session_factory, send_message, *, storage="shared"):
    if storage != "shared":
        raise ValueError("Поддерживается только общая схема korsik; legacy-режим удалён")
    from .scheduler import run_delivery_tick, run_scheduler_tick
    @celery_app.task(name="onboarding.schedule", ignore_result=True)
    def schedule():
        return run_scheduler_tick(session_factory)

    @celery_app.task(name="onboarding.deliver", ignore_result=True, queue="onboarding.delivery")
    def deliver():
        return run_delivery_tick(session_factory, send_message)

    # Сохраняем остальные периодические задачи общего приложения.
    celery_app.conf.beat_schedule = {
        **(celery_app.conf.beat_schedule or {}),
        "onboarding-schedule-every-30-seconds": {"task": "onboarding.schedule", "schedule": 30.0},
        "onboarding-delivery-every-5-seconds": {"task": "onboarding.deliver", "schedule": 5.0,
                                                "options": {"queue": "onboarding.delivery"}},
    }
    return {"schedule": schedule, "deliver": deliver}
