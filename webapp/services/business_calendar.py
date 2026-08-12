"""
The one shared definition of "today" for same-day edit/void eligibility
(Operator same-day editing window) — Africa/Kampala (EAT), a fixed
UTC+3 offset that has never observed daylight saving time, so a plain
fixed-offset `timezone` is exact here and needs no IANA tzdata package
(confirmed unavailable in this dev environment — `zoneinfo.ZoneInfo
("Africa/Kampala")` raises ZoneInfoNotFoundError without the `tzdata`
package installed, which system Python on Windows does not ship).

Never use `datetime.now(timezone.utc)` for this — near local midnight
that can silently disagree with the Kampala calendar date by one day.
Every record's own `date` column is already a plain "YYYY-MM-DD"
business-date string (see e.g. return_record.py), so comparing it to
this function's output is a plain string equality, never a datetime
comparison.
"""
from datetime import datetime, timedelta, timezone

KAMPALA_OFFSET = timezone(timedelta(hours=3))


def business_today():
    """Today's date (YYYY-MM-DD) in Africa/Kampala, right now."""
    return datetime.now(KAMPALA_OFFSET).strftime("%Y-%m-%d")


def is_same_business_day(date_str):
    return date_str == business_today()


def format_kampala_datetime(dt):
    """
    Human-readable Africa/Kampala rendering of a naive-UTC datetime column
    (e.g. Dispatch/ReturnRecord/ProductionRecord.created_at) for read-only
    History/reporting display — "12 Aug 2026, 3:42 PM". Display formatting
    only, never used for business-date comparisons (see
    is_same_business_day() above) and never written back to any record.

    Deliberately avoids the platform-dependent %-d/%-I strftime directives
    (no leading-zero stripping) — those work on Linux/macOS but raise on
    Windows' strftime, and this app's tests run on a Windows dev machine
    even though the deployed container is Linux.

    Returns None if `dt` is None — the caller decides the neutral
    placeholder (e.g. "—") for a record with no usable timestamp.
    """
    if dt is None:
        return None
    local = dt.replace(tzinfo=timezone.utc).astimezone(KAMPALA_OFFSET)
    hour12 = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{local.day} {local.strftime('%b %Y')}, {hour12}:{local.strftime('%M')} {ampm}"
