"""
Spare Parts consumption analytics — pure aggregation over the authoritative
SparePartMovement ledger (webapp/services/spare_part_movement_service.py
derives current stock from the same ledger; this module derives historical
usage from it). Consumption is ALWAYS movement_type=STOCK_OUT with
status=active: adjustments (corrections/counts), opening balances, receipts,
and voided rows never count as usage, so none of them can inflate demand
forecasts (see SparePartMovement's own docstring for why those are distinct
movement types in the first place).

Nothing here touches pricing, procurement math, permissions, or the HTTP
layer — see spare_part_planning_service.py for those. Every bulk_* function
takes a list of spare_part_ids and returns one aggregated query's worth of
results, never one query per part (Consumption & Procurement Planning may
be rendering hundreds of rows at once).
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func

from webapp.extensions import db
from webapp.models.spare_part_movement import MOVEMENT_STOCK_OUT, STATUS_ACTIVE, SparePartMovement

# ---------- configuration (see docs/SPARE-PARTS-PLANNING.md) ----------

# A single real month (30 days) is always the minimum divisor for a
# per-month average, no matter how little elapsed history exists. Without
# this floor, a handful of days of history would be scaled up by ~30x into
# a wildly exaggerated monthly rate — exactly the naive mistake the feature
# must avoid (see average_monthly_consumption()'s docstring below).
MINIMUM_AVERAGING_DAYS = 30

# Recent-vs-preceding trend threshold, as a percentage change. Smaller
# fluctuations are "Stable" rather than flapping between labels on noise.
TREND_THRESHOLD_PERCENT = Decimal("15")


def parse_date(value):
    """Accepts a date or a 'YYYY-MM-DD' string; always returns a date."""
    return value if isinstance(value, date) else date.fromisoformat(value)


def date_str(d):
    return d.isoformat()


def window_bounds(end_date, days):
    """Inclusive [start, end] business-date strings for a trailing N-day
    window ending on end_date (a date or 'YYYY-MM-DD' string)."""
    end = parse_date(end_date)
    start = end - timedelta(days=days - 1)
    return date_str(start), date_str(end)


def preceding_window_bounds(end_date, days):
    """The equal-length window immediately BEFORE window_bounds(end_date,
    days) — never overlapping, never a different length (section 9 of the
    spec: "Do not compare unequal periods")."""
    end = parse_date(end_date)
    recent_start = end - timedelta(days=days - 1)
    preceding_end = recent_start - timedelta(days=1)
    preceding_start = preceding_end - timedelta(days=days - 1)
    return date_str(preceding_start), date_str(preceding_end)


def bulk_consumption(spare_part_ids, date_from, date_to, movement_type=MOVEMENT_STOCK_OUT):
    """Sum of `movement_type` (default STOCK_OUT) quantity per spare part,
    status=active only, within [date_from, date_to] inclusive. One
    aggregated query for however many ids are passed; ids with no matching
    rows are present in the result as Decimal('0'), never missing."""
    ids = list(spare_part_ids)
    totals = {pid: Decimal("0") for pid in ids}
    if not ids:
        return totals
    rows = (
        db.session.query(SparePartMovement.spare_part_id, func.sum(SparePartMovement.quantity))
        .filter(
            SparePartMovement.spare_part_id.in_(ids),
            SparePartMovement.status == STATUS_ACTIVE,
            SparePartMovement.movement_type == movement_type,
            SparePartMovement.business_date >= date_from,
            SparePartMovement.business_date <= date_to,
        )
        .group_by(SparePartMovement.spare_part_id)
        .all()
    )
    for spare_part_id, total in rows:
        totals[spare_part_id] = Decimal(total) if total is not None else Decimal("0")
    return totals


def bulk_first_ledger_date(spare_part_ids):
    """Earliest business_date (any status/movement type) per spare part —
    the actual observable start of that part's ledger history, used to cap
    averaging windows so a brand-new part's few days of data are never
    stretched to look like a full historical period. Parts with zero
    ledger rows are absent from the result (caller falls back to the
    part's own created_at)."""
    ids = list(spare_part_ids)
    if not ids:
        return {}
    rows = (
        db.session.query(SparePartMovement.spare_part_id, func.min(SparePartMovement.business_date))
        .filter(SparePartMovement.spare_part_id.in_(ids))
        .group_by(SparePartMovement.spare_part_id)
        .all()
    )
    return {pid: earliest for pid, earliest in rows if earliest is not None}


def bulk_last_active_stock_out_date(spare_part_ids):
    """Most recent STOCK_OUT (status=active) business_date per spare part —
    used by slow-moving/dead-stock classification. Absent from the result
    means that part has never had a genuine, active sign-out."""
    ids = list(spare_part_ids)
    if not ids:
        return {}
    rows = (
        db.session.query(SparePartMovement.spare_part_id, func.max(SparePartMovement.business_date))
        .filter(
            SparePartMovement.spare_part_id.in_(ids),
            SparePartMovement.status == STATUS_ACTIVE,
            SparePartMovement.movement_type == MOVEMENT_STOCK_OUT,
        )
        .group_by(SparePartMovement.spare_part_id)
        .all()
    )
    return {pid: latest for pid, latest in rows if latest is not None}


def history_days_available(history_start, as_of):
    """
    Whole days of OBSERVABLE history between history_start and as_of
    (inclusive of both ends, so a part whose only movement was today has
    1 day of history, never 0). None if there is no history at all.
    """
    if history_start is None:
        return None
    start = parse_date(history_start)
    end = parse_date(as_of)
    return max(1, (end - start).days + 1)


def average_monthly_consumption(total_quantity, window_days):
    """
    The one defensible "average monthly consumption" formula used
    throughout this feature: total_quantity consumed across window_days,
    divided by max(1, window_days / 30) month-equivalents.

    The floor at 1 month-equivalent is the important part. Without it, a
    naive total/days*30 projection would take a handful of days of real
    history and scale it up by as much as ~30x into a wildly exaggerated
    monthly rate — e.g. 50 units issued 2 days after a part was created
    would naively "forecast" 750/month. Flooring the divisor at 1 instead
    reports that same 50 units as the monthly rate until a genuine month
    of history exists to refine it — conservative, not exaggerated.

    A full year of history divides by ~12.17 (365/30), not a flat 12, so
    the constant stays internally consistent with the 30-day month used
    everywhere else in this module. Handles window_days<=0 and
    total_quantity==0 by returning Decimal('0') — never NaN/Infinity.
    """
    if window_days is None or window_days <= 0:
        return Decimal("0")
    months = max(Decimal("1"), Decimal(window_days) / Decimal(MINIMUM_AVERAGING_DAYS))
    return Decimal(total_quantity) / months


@dataclass(frozen=True)
class TrendResult:
    label: str  # "Increasing" | "Decreasing" | "Stable" | "Insufficient Data"
    recent_total: Decimal
    preceding_total: Decimal
    percent_change: Decimal | None


def classify_trend(recent_total, preceding_total, *, has_full_preceding_window):
    """
    Increasing/Decreasing/Stable/Insufficient Data, comparing two EQUAL-
    LENGTH, non-overlapping periods (see preceding_window_bounds()).
    `has_full_preceding_window` must be False whenever the part's observed
    history does not reach back far enough to cover the entire preceding
    period — comparing a real period against a partially-populated one
    would misreport a part that is simply new as "Decreasing".
    """
    if not has_full_preceding_window:
        return TrendResult("Insufficient Data", recent_total, preceding_total, None)
    if preceding_total == 0 and recent_total == 0:
        return TrendResult("Stable", recent_total, preceding_total, Decimal("0"))
    if preceding_total == 0:
        return TrendResult("Increasing", recent_total, preceding_total, None)
    percent_change = (recent_total - preceding_total) / preceding_total * Decimal("100")
    if percent_change > TREND_THRESHOLD_PERCENT:
        label = "Increasing"
    elif percent_change < -TREND_THRESHOLD_PERCENT:
        label = "Decreasing"
    else:
        label = "Stable"
    return TrendResult(label, recent_total, preceding_total, percent_change)


def forecast_monthly_demand(average_monthly_consumption_value, history=None):
    """
    The pluggable forecasting seam the spec asks for: today's baseline
    forecast IS the defensible historical average monthly consumption.
    `history` is accepted (currently unused) purely so a future, smarter
    method — seasonal adjustment, weighting recent months more heavily,
    external demand signals — can replace the body of this one function
    without any caller, route, or template needing to change.
    """
    return average_monthly_consumption_value
