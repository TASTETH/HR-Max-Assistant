"""Working calendar and due-time calculation for scenario anchors.

Rules (documented in RUNBOOK):
  * workday n: n-th working day counting the start date as day 1 (Mon–Fri minus HOLIDAYS);
  * days / months: calendar offsets from the start date (relativedelta for months),
    moved forward to the next working day if they land on a day off;
  * before_start: N calendar days before the start date (sent even on weekends — it is a
    reminder before the first day);
  * probation_end: HR-confirmed date or start + 3 months − 1 day, moved back to a working day.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

# Non-working public holidays (RF production calendar). Extend in config for new years.
HOLIDAYS = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
    date(2026, 1, 8), date(2026, 1, 9), date(2026, 2, 23), date(2026, 3, 9), date(2026, 5, 1),
    date(2026, 5, 11), date(2026, 6, 12), date(2026, 11, 4), date(2026, 12, 31),
    # 2027 (fixed dates; transfers to be confirmed by the official calendar)
    date(2027, 1, 1), date(2027, 1, 4), date(2027, 1, 5), date(2027, 1, 6), date(2027, 1, 7),
    date(2027, 1, 8), date(2027, 2, 23), date(2027, 3, 8), date(2027, 5, 3), date(2027, 5, 10),
    date(2027, 6, 14), date(2027, 11, 4),
}


def is_workday(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def next_workday(d: date) -> date:
    while not is_workday(d):
        d += timedelta(days=1)
    return d


def prev_workday(d: date) -> date:
    while not is_workday(d):
        d -= timedelta(days=1)
    return d


def nth_workday(start: date, n: int) -> date:
    d = next_workday(start)
    count = 1
    while count < n:
        d += timedelta(days=1)
        if is_workday(d):
            count += 1
    return d


def default_probation_end(start: date) -> date:
    return start + relativedelta(months=3) - timedelta(days=1)


def _parse_time(value: str) -> time:
    h, m = value.replace(".", ":").split(":")
    return time(int(h), int(m))


def due_at(anchor: dict, *, start: date, probation_end: date | None, tz: str,
           default_time: str = "10:00") -> datetime:
    kind = anchor["type"]
    t = _parse_time(anchor.get("time") or default_time)
    if kind == "workday":
        d = nth_workday(start, anchor["n"])
    elif kind == "before_start":
        d = start - timedelta(days=anchor["days"])
    elif kind == "days":
        d = next_workday(start + timedelta(days=anchor["days"]))
    elif kind == "months":
        d = next_workday(start + relativedelta(months=anchor["months"], days=anchor.get("days", 0)))
    elif kind == "probation_end":
        d = prev_workday(probation_end or default_probation_end(start))
    else:
        raise ValueError(f"unknown anchor {kind}")
    return datetime.combine(d, t, tzinfo=ZoneInfo(tz))


def next_local(dt_utc: datetime, tz: str, at: str, days: int = 1) -> datetime:
    """Local calendar rule for reminders: `days` later at `at` local time."""
    local = dt_utc.astimezone(ZoneInfo(tz))
    d = local.date() + timedelta(days=days)
    return datetime.combine(d, _parse_time(at), tzinfo=ZoneInfo(tz))
