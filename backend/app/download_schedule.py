from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_clock(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Download times must use HH:MM format.") from error
    if parsed.second or parsed.microsecond:
        raise ValueError("Download times must use minute precision.")
    return parsed.replace(tzinfo=None)


def validate_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    timezone_name = str(schedule.get("timezone") or "").strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Choose a valid IANA browser timezone.") from error

    weekdays = sorted({int(day) for day in schedule.get("weekdays") or []})
    if any(day < 0 or day > 6 for day in weekdays):
        raise ValueError("Weekdays must use ISO values from 0 (Monday) to 6 (Sunday).")
    start = parse_clock(str(schedule.get("start_time") or ""))
    end = parse_clock(str(schedule.get("end_time") or ""))
    if start == end:
        raise ValueError("Download window start and end times must differ.")
    if schedule.get("enabled") and not weekdays:
        raise ValueError("Choose at least one weekday for the download window.")
    return {
        "enabled": bool(schedule.get("enabled")),
        "timezone": timezone_name,
        "weekdays": weekdays,
        "start_time": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"),
    }


def window_is_open(schedule: dict[str, Any], now: datetime | None = None) -> bool:
    if not schedule.get("enabled"):
        return True
    validated = validate_schedule(schedule)
    current = (now or datetime.now(timezone.utc)).astimezone(
        ZoneInfo(validated["timezone"])
    )
    current_time = current.time().replace(tzinfo=None)
    start = parse_clock(validated["start_time"])
    end = parse_clock(validated["end_time"])
    weekdays = set(validated["weekdays"])
    if start < end:
        return current.weekday() in weekdays and start <= current_time < end
    if current_time >= start:
        return current.weekday() in weekdays
    return current_time < end and (current.weekday() - 1) % 7 in weekdays
