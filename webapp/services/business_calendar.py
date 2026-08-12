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
