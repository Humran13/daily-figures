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


def utcnow():
    """
    The one shared "current instant" for anything storing/comparing a
    naive-UTC timestamp column (created_at/reviewed_at/completed_at
    etc. — matches every model's own private `_utcnow()` exactly, just
    centralized here so the 24-hour edit-window/grant-expiry math in
    record_correction_service.py and correction_request_service.py has a
    single source instead of each duplicating datetime.now(timezone.utc)
    .replace(tzinfo=None)). Callers that need deterministic tests accept
    an explicit `now` parameter instead of calling this directly inside
    the comparison itself — this function is only ever the production
    default.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def business_today(now=None):
    """
    Today's date (YYYY-MM-DD) in Africa/Kampala. `now` is accepted only
    for deterministic tests (a naive-UTC datetime, same convention as
    utcnow()) — production code always omits it and gets the real
    current instant, matching every other `now=None`-style function in
    this module/record_correction_service.py/correction_request_service.py.
    """
    if now is None:
        return datetime.now(KAMPALA_OFFSET).strftime("%Y-%m-%d")
    return now.replace(tzinfo=timezone.utc).astimezone(KAMPALA_OFFSET).strftime("%Y-%m-%d")


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


def format_kampala_report_timestamp(dt=None):
    """
    "YYYY-MM-DD HH:MM EAT" — the fixed "Generated ..." header format every
    export/report uses (see webapp/services/export_service.py). A
    distinct format from format_kampala_datetime() above (which is for
    reading a stored record's own history-display timestamp, "12 Aug
    2026, 3:42 PM") because report headers have always used this
    different, more compact style — this only fixes WHICH timezone that
    existing style renders in, never its shape.

    dt: a naive-UTC datetime (same convention as format_kampala_datetime());
    omit it to use the current instant (the common "generated right now"
    case) — accepts an explicit value only so tests can pass a fixed,
    deterministic instant instead of the real wall clock.
    """
    dt = dt if dt is not None else utcnow()
    local = dt.replace(tzinfo=timezone.utc).astimezone(KAMPALA_OFFSET)
    return local.strftime("%Y-%m-%d %H:%M") + " EAT"
