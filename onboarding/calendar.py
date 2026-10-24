"""Расчёт проекта расписания до/после выхода. Записи в общую БД не создаются.

WorkCalendar передаёт backend. Сохранение расширенных правил в общей схеме
ожидает согласования из DB_COMPATIBILITY.md.
"""
import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import FrozenSet
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class WorkCalendar:
    weekdays: FrozenSet[int] = field(default_factory=lambda: frozenset(range(5)))
    holidays: FrozenSet[date] = frozenset()
    extra_workdays: FrozenSet[date] = frozenset()

    def __post_init__(self):
        if not self.weekdays or not self.weekdays <= set(range(7)):
            raise ValueError("В календаре нужны номера рабочих дней от 0 до 6")

    def is_workday(self, day):
        return day in self.extra_workdays or (day.weekday() in self.weekdays and day not in self.holidays)

    def nth_day(self, start, number):
        if type(number) is not int or number < 1:
            raise ValueError("Номер рабочего дня должен быть положительным целым числом")
        if not self.is_workday(start):
            raise ValueError("Дата выхода не является рабочей по переданному календарю")
        current, passed = start, 1
        while passed < number:
            current += timedelta(days=1)
            if self.is_workday(current):
                passed += 1
        return current

    def as_dict(self):
        return {"weekdays": sorted(self.weekdays), "holidays": sorted(d.isoformat() for d in self.holidays),
                "extra_workdays": sorted(d.isoformat() for d in self.extra_workdays)}


def add_months(day, count):
    year, month = divmod(day.year * 12 + day.month - 1 + count, 12)
    month += 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def event_dates(rule, start_date, timezone_name, work_calendar=None, probation_end_date=None):
    zone = ZoneInfo(timezone_name)
    if rule.get("anchor", "start_date") not in ("start_date", "probation_end_date"):
        raise ValueError("Неизвестный якорь расписания")
    anchor = start_date
    if rule.get("anchor", "start_date") == "probation_end_date":
        if probation_end_date is None:
            raise ValueError("HR должен передать дату окончания испытательного срока")
        if probation_end_date < start_date:
            raise ValueError("Конец испытательного срока раньше даты выхода")
        anchor = probation_end_date
    if "workday" in rule:
        if work_calendar is None:
            raise ValueError("Для рабочих дней нужен явный WorkCalendar сотрудника")
        day = work_calendar.nth_day(anchor, rule["workday"])
    else:
        day = add_months(anchor, rule.get("months", 0)) + timedelta(days=rule.get("days", 0))
    due = datetime.combine(day, time.fromisoformat(rule["time"]), zone)
    if rule.get("phase") == "preboarding":
        # Устаревшие напоминания «завтра выходить» не отправляются после выхода.
        expires = datetime.combine(start_date, time.min, zone)
    else:
        expires = datetime.combine(day + timedelta(days=rule.get("valid_days", 1)), time.min, zone)
    return due.astimezone(timezone.utc), expires.astimezone(timezone.utc)


def materialize(catalog, start_date, *, location_tag, employee_type, timezone_name="Europe/Moscow",
                work_calendar=None, probation_end_date=None):
    if location_tag not in catalog["location_tags"]:
        raise ValueError("Неизвестный тег площадки: " + str(location_tag))
    if employee_type not in ("office", "production"):
        raise ValueError("Нужен employee_type: office или production")
    result = []
    for event in catalog["events"]:
        audience = event.get("audience", {})
        if audience.get("location_tags") and location_tag not in audience["location_tags"]:
            continue
        if audience.get("employee_types") and employee_type not in audience["employee_types"]:
            continue
        definition = event
        if "variants" in event:
            definition = {**event, **event["variants"][location_tag]}
            definition.pop("variants", None)
        due, expires = event_dates({**definition["schedule"], "phase": definition["phase"]},
                                  start_date, timezone_name, work_calendar, probation_end_date)
        result.append({"code": definition["code"], "kind": "survey" if definition.get("questions") else "message",
                       "text": definition.get("text", ""), "phase": definition["phase"],
                       "required": definition.get("required", False), "definition": definition,
                       "due_at": due, "expires_at": expires})
    # Порядок файла не заменяет хронологический порядок событий.
    result.sort(key=lambda event: (event["due_at"], event["definition"]["source_rows"][0]))
    for position, event in enumerate(result):
        event["position"] = position
    return result
