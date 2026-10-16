"""
Consumption & Procurement Planning — the procurement-math and orchestration
layer. spare_part_analytics_service.py supplies the pure consumption/trend
aggregation; spare_part_service.search_spare_parts() supplies the one shared
name/specification search; this module adds safety stock, recommended
purchase, stock coverage, ABC classification, procurement priority,
slow-moving/dead-stock, and China/Local/Best-Price costing on top, then
ties it all together into the rows + summary the route/export layer serve.

See docs/SPARE-PARTS-PLANNING.md for the full methodology writeup. Every
constant below is deliberately centralized here (never duplicated in a
template or route) so the formulas stay auditable from one place.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal

from webapp.extensions import db
from webapp.models.machine import Machine
from webapp.models.spare_part import (
    COST_SOURCE_CHINA, COST_SOURCE_LOCAL, SparePart, SparePartMachine, SparePartSupplier,
)
from webapp.models.spare_part_category import SparePartCategory
from webapp.models.spare_part_movement import MOVEMENT_STOCK_OUT, STATUS_ACTIVE, SparePartMovement
from webapp.models.operational_department import SparePartDepartment
from webapp.services import spare_part_analytics_service as analytics
from webapp.services import spare_part_movement_service as movement_svc
from webapp.services import spare_part_service as sp_service
from webapp.services.business_calendar import business_today

# ---------- configuration ----------
# Deliberately small: only the numbers that genuinely need to be tunable
# are here, per the spec's own "do not make every number configurable"
# guidance. Everything else (search, filters, sourcing) is structural, not
# a threshold.

HORIZON_CHOICES = (3, 6, 9, 12)
DEFAULT_HORIZON_MONTHS = 12
DEFAULT_ANALYSIS_PERIOD_DAYS = 365
ANALYSIS_PERIOD_CHOICES = (30, 90, 180, 365)
TRAILING_WINDOWS_DAYS = (30, 90, 180, 365)

SOURCE_CHINA = "china"
SOURCE_LOCAL = "local_uganda"
SOURCE_BEST_PRICE = "best_price"
SOURCE_CHOICES = (SOURCE_CHINA, SOURCE_LOCAL, SOURCE_BEST_PRICE)

# Default safety stock, used only when a part has no safety_stock_override
# set. minimum_stock/reorder level and safety stock are DIFFERENT concepts
# — minimum_stock drives the existing low-stock alert and is never read
# here (see safety_stock_for()'s docstring) — so this uses its own
# dedicated, nullable column rather than conflating the two.
DEFAULT_SAFETY_STOCK_MONTHS = Decimal("1")

# This application has no purchase-order/pending-receipt system yet.
# Flipping this to True (once a real source is wired in) is the only
# change needed for incoming_stock_display() to start showing the real
# quantity instead of "Not tracked" — see confirmed_incoming_stock_for().
INCOMING_STOCK_TRACKED = False

# A row without enough price information to be ranked by consumption
# value is explicitly Unclassified — never silently given a fabricated
# "C" (low-value) class it was never actually shown to deserve (see
# classify_abc()).
ABC_UNCLASSIFIED = "Unclassified"

# Stock-coverage thresholds (in months) that drive procurement priority —
# see procurement_priority()'s docstring for the full rule cascade.
URGENT_COVERAGE_MONTHS = Decimal("1")
HIGH_COVERAGE_MONTHS = Decimal("2")
MEDIUM_COVERAGE_MONTHS = Decimal("4")

# A part is Slow Moving once its current stock covers more than this many
# months of average demand (and it still has SOME recent consumption —
# zero-consumption-with-stock is Dead Stock instead, a distinct state).
SLOW_MOVING_COVERAGE_MONTHS = Decimal("12")

# Dead Stock requires BOTH: the part is at least this many days old (so a
# brand-new part with stock and simply no sign-outs YET is never
# mislabeled dead) AND no active Stock Out within that same window.
DEAD_STOCK_DAYS = 365

# Cap the "estimated stockout date" horizon — beyond this, the exact
# calendar date stops being a meaningful planning estimate; the UI shows
# "beyond planning horizon" instead of an absurd far-future date.
MAX_STOCKOUT_PROJECTION_DAYS = 3650  # 10 years

# ABC: value-based classification requires usable pricing for at least
# this fraction of the filtered, in-scope rows; otherwise the whole report
# falls back to quantity-based ABC (never a per-row mix — that would not
# be a defensible, explainable methodology — see classify_abc()).
ABC_MIN_PRICE_COVERAGE = Decimal("0.5")
ABC_A_CUTOFF_PERCENT = Decimal("80")
ABC_B_CUTOFF_PERCENT = Decimal("95")

PRIORITY_URGENT, PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW = "Urgent", "High", "Medium", "Low"

STATUS_STOCKOUT, STATUS_BELOW_MINIMUM, STATUS_HEALTHY = "stockout", "below_minimum", "healthy"

VIEW_MOST_CONSUMED = "most_consumed"
VIEW_FASTEST_MOVING = "fastest_moving"
VIEW_INCREASING = "increasing_consumption"
VIEW_STOCKOUT_RISK = "stockout_risk"
VIEW_BELOW_MINIMUM = "below_minimum_stock"
VIEW_CRITICAL = "critical_parts"
VIEW_HIGHEST_VALUE = "highest_procurement_value"
VIEW_CHINA_SAVINGS = "china_savings_opportunity"
VIEW_SLOW_MOVING = "slow_moving"
VIEW_DEAD_STOCK = "dead_stock"
VIEWS = (
    VIEW_MOST_CONSUMED, VIEW_FASTEST_MOVING, VIEW_INCREASING, VIEW_STOCKOUT_RISK,
    VIEW_BELOW_MINIMUM, VIEW_CRITICAL, VIEW_HIGHEST_VALUE, VIEW_CHINA_SAVINGS,
    VIEW_SLOW_MOVING, VIEW_DEAD_STOCK,
)


def _usable(value):
    return value is not None and value > 0


def _quantize_qty(value):
    return value.quantize(Decimal("0.001"))


def _quantize_money(value):
    return value.quantize(Decimal("0.01")) if value is not None else None


# ---------- safety stock / recommended purchase / coverage ----------

def safety_stock_for(safety_stock_override, average_monthly_consumption_value):
    """
    Safety stock is DELIBERATELY independent of minimum_stock. minimum_stock
    is the existing reorder-level/low-stock-alert field
    (is_low_stock()/stock_status_for()) and keeps meaning exactly that,
    completely unaffected by this function. safety_stock_override is its
    own nullable column (migration d4f6a8b0c2e4): when a part has one
    set, it is used as-is; when NULL (every part by default), the
    application default rule applies instead — DEFAULT_SAFETY_STOCK_MONTHS
    worth of that part's own average monthly consumption. Never negative.
    """
    if safety_stock_override is not None:
        return safety_stock_override
    return average_monthly_consumption_value * DEFAULT_SAFETY_STOCK_MONTHS


def confirmed_incoming_stock_for(spare_part):
    """
    The application has no purchase-order/pending-receipt concept yet, so
    confirmed incoming stock is always zero for the FORECAST MATH —
    architected as its own function (rather than a literal 0 inlined at
    every call site) so a real incoming-stock source can be wired in here
    later without touching recommended_purchase(), the route, or the
    template. See incoming_stock_display() for the human-facing
    representation, which must never imply this zero was verified.
    """
    return Decimal("0")


def incoming_stock_display(incoming_stock_value):
    """
    Human-facing (UI/export) representation of incoming stock. While
    INCOMING_STOCK_TRACKED is False, always "Not tracked" — a bare "0"
    would wrongly imply the system verified there are no pending purchase
    orders, when in fact it simply has no way to know. Once a real
    incoming-stock source is wired in and INCOMING_STOCK_TRACKED is
    flipped to True, this starts reporting the actual quantity instead —
    the only change needed anywhere in the UI/export layer.
    """
    if not INCOMING_STOCK_TRACKED:
        return "Not tracked"
    return str(incoming_stock_value.quantize(Decimal("0.001")))


def recommended_purchase(projected_demand, safety_stock, current_stock, incoming_stock):
    """
    recommended_purchase = projected_demand + safety_stock -
    current_usable_stock - confirmed_incoming_stock, clamped to a minimum
    of zero (never a negative "recommended purchase").
    """
    raw = projected_demand + safety_stock - current_stock - incoming_stock
    return raw if raw > Decimal("0") else Decimal("0")


def stock_coverage_months(current_stock, monthly_demand):
    """Months of cover current stock represents at the given monthly
    demand rate. None (not zero, not Infinity) when demand is zero —
    the caller/template renders that as "No recent consumption"."""
    if monthly_demand is None or monthly_demand <= 0:
        return None
    return current_stock / monthly_demand


def estimated_stockout_date(current_stock, monthly_demand, as_of):
    """
    A planning ESTIMATE, not a guarantee — labeled as such by every
    caller. None when demand is zero (nothing to project). Capped at
    MAX_STOCKOUT_PROJECTION_DAYS out; beyond that the caller shows
    "beyond planning horizon" rather than a meaningless far-future date.
    """
    if monthly_demand is None or monthly_demand <= 0:
        return None
    as_of_date = analytics.parse_date(as_of)
    if current_stock <= 0:
        return analytics.date_str(as_of_date)
    coverage_months = current_stock / monthly_demand
    days = int((coverage_months * Decimal(30)).to_integral_value(rounding=ROUND_FLOOR))
    if days > MAX_STOCKOUT_PROJECTION_DAYS:
        return None
    return analytics.date_str(as_of_date + timedelta(days=days))


def stock_status_for(current_stock, minimum_stock):
    if current_stock <= 0:
        return STATUS_STOCKOUT
    if minimum_stock is not None and current_stock <= minimum_stock:
        return STATUS_BELOW_MINIMUM
    return STATUS_HEALTHY


def classify_movement_speed(*, current_stock, history_days, days_since_last_stock_out, coverage_months):
    """
    Returns (is_dead_stock, is_slow_moving) — mutually exclusive.

    Dead Stock: stock currently on hand AND the part is old enough to
    judge (history_days >= DEAD_STOCK_DAYS) AND there has been no active
    Stock Out within that same window (days_since_last_stock_out is None,
    meaning "never issued", or >= DEAD_STOCK_DAYS). A part younger than
    DEAD_STOCK_DAYS is never Dead Stock even with zero sign-outs so far —
    it is simply new, not proven dead (spec section 19/30).

    Slow Moving: stock on hand with SOME consumption history (otherwise
    it would already be Dead Stock above) but current coverage is
    unusually long relative to its own demand (> SLOW_MOVING_COVERAGE_MONTHS).
    """
    if current_stock <= 0:
        return False, False
    if history_days is not None and history_days >= DEAD_STOCK_DAYS and (
        days_since_last_stock_out is None or days_since_last_stock_out >= DEAD_STOCK_DAYS
    ):
        return True, False
    if coverage_months is not None and coverage_months > SLOW_MOVING_COVERAGE_MONTHS:
        return False, True
    return False, False


# ---------- ABC classification ----------

@dataclass(frozen=True)
class ABCResult:
    class_by_id: dict
    methodology: str  # "value" | "quantity"
    price_coverage_ratio: Decimal


def classify_abc(rows):
    """
    rows: iterable of {"id", "annual_quantity": Decimal, "unit_cost": Decimal|None}.

    Prefers consumption-VALUE ABC (annual_quantity * unit_cost, Pareto
    cumulative-percentage cutoffs at ABC_A_CUTOFF_PERCENT/
    ABC_B_CUTOFF_PERCENT). Falls back to a quantity-only ranking for the
    WHOLE report (never a per-row mix, which would not be a defensible,
    explainable single methodology) whenever fewer than
    ABC_MIN_PRICE_COVERAGE of the rows have a usable unit cost.

    A missing price is NEVER treated as zero value, and is never silently
    folded into class C either — "C" means "genuinely low consumption
    value", which is a claim this function cannot make about a row whose
    value it cannot compute. Under value-based ABC, a row with no usable
    cost is given the explicit ABC_UNCLASSIFIED class instead, so a
    high-consumption item with simply no price on file is never
    misread as low-priority. (Under the quantity-based fallback, every
    row participates in the ranking regardless of pricing, so
    Unclassified never applies there.)
    """
    rows = list(rows)
    if not rows:
        return ABCResult({}, "quantity", Decimal("0"))

    priced = [r for r in rows if r["unit_cost"] is not None]
    coverage = Decimal(len(priced)) / Decimal(len(rows))
    use_value = coverage >= ABC_MIN_PRICE_COVERAGE and len(priced) > 0

    class_by_id = {}
    if use_value:
        ranked = sorted(priced, key=lambda r: r["annual_quantity"] * r["unit_cost"], reverse=True)
        total_value = sum((r["annual_quantity"] * r["unit_cost"] for r in ranked), Decimal("0"))
        cumulative = Decimal("0")
        for r in ranked:
            cumulative += r["annual_quantity"] * r["unit_cost"]
            pct = (cumulative / total_value * Decimal("100")) if total_value > 0 else Decimal("100")
            class_by_id[r["id"]] = _bucket_for(pct)
        for r in rows:
            class_by_id.setdefault(r["id"], ABC_UNCLASSIFIED)  # no usable price: never ranked, never fabricated
    else:
        ranked = sorted(rows, key=lambda r: r["annual_quantity"], reverse=True)
        total_qty = sum((r["annual_quantity"] for r in ranked), Decimal("0"))
        cumulative = Decimal("0")
        for r in ranked:
            cumulative += r["annual_quantity"]
            pct = (cumulative / total_qty * Decimal("100")) if total_qty > 0 else Decimal("100")
            class_by_id[r["id"]] = _bucket_for(pct)

    return ABCResult(class_by_id, "value" if use_value else "quantity", coverage)


def _bucket_for(cumulative_percent):
    if cumulative_percent <= ABC_A_CUTOFF_PERCENT:
        return "A"
    if cumulative_percent <= ABC_B_CUTOFF_PERCENT:
        return "B"
    return "C"


# ---------- procurement priority ----------

def procurement_priority(*, stock_status, coverage_months, critical, trend_label):
    """
    Deterministic rule cascade (first match wins) combining stock
    position, projected coverage, criticality, and trend — never a hidden
    template conditional. A low-consumption part can still reach Urgent/
    High purely through `critical`, since criticality must influence
    priority without touching historical consumption figures (spec
    section 16).

      1. Already out of stock                         -> Urgent
      2. Critical AND <= URGENT_COVERAGE_MONTHS cover  -> Urgent
      3. Below minimum stock                           -> High
      4. <= HIGH_COVERAGE_MONTHS cover                 -> High
      5. Critical AND consumption trend is Increasing  -> High
      6. <= MEDIUM_COVERAGE_MONTHS cover                -> Medium
      7. Critical (any remaining case)                 -> Medium
      8. Everything else                                -> Low
    """
    if stock_status == STATUS_STOCKOUT:
        return PRIORITY_URGENT
    if critical and coverage_months is not None and coverage_months <= URGENT_COVERAGE_MONTHS:
        return PRIORITY_URGENT
    if stock_status == STATUS_BELOW_MINIMUM:
        return PRIORITY_HIGH
    if coverage_months is not None and coverage_months <= HIGH_COVERAGE_MONTHS:
        return PRIORITY_HIGH
    if critical and trend_label == "Increasing":
        return PRIORITY_HIGH
    if coverage_months is not None and coverage_months <= MEDIUM_COVERAGE_MONTHS:
        return PRIORITY_MEDIUM
    if critical:
        return PRIORITY_MEDIUM
    return PRIORITY_LOW


# ---------- China / Local / Best Price costing ----------

def best_price_selection(china_price, local_price):
    """
    Cheaper VALID source between China and Local. Never substitutes one
    for the other when a price is simply missing — returns (None, None)
    only when BOTH are unusable, (source, price) using whichever single
    one IS usable when only one is, and the genuinely cheaper of the two
    (China wins an exact tie, an arbitrary but documented choice) when
    both are usable.
    """
    china_ok, local_ok = _usable(china_price), _usable(local_price)
    if not china_ok and not local_ok:
        return None, None
    if not china_ok:
        return COST_SOURCE_LOCAL, local_price
    if not local_ok:
        return COST_SOURCE_CHINA, china_price
    return (COST_SOURCE_CHINA, china_price) if china_price <= local_price else (COST_SOURCE_LOCAL, local_price)


def procurement_cost(quantity, unit_price):
    """quantity * unit_price, or None when unit_price is missing — never
    silently substituted with a different source's price or with zero."""
    if unit_price is None:
        return None
    return quantity * unit_price


# ---------- orchestration ----------

def _ids_with_machine(machine_id):
    rows = db.session.query(SparePartMachine.spare_part_id).filter_by(machine_id=machine_id).all()
    return {r[0] for r in rows}


def _ids_with_department_history(department_id):
    rows = (
        db.session.query(SparePartMovement.spare_part_id)
        .filter(
            SparePartMovement.department_id == department_id,
            SparePartMovement.status == STATUS_ACTIVE,
            SparePartMovement.movement_type == MOVEMENT_STOCK_OUT,
        )
        .distinct()
        .all()
    )
    return {r[0] for r in rows}


def _ids_with_supplier(supplier_id):
    rows = db.session.query(SparePartSupplier.spare_part_id).filter_by(supplier_id=supplier_id).all()
    return {r[0] for r in rows}


def build_planning_report(
    *, as_of=None, horizon_months=DEFAULT_HORIZON_MONTHS, analysis_period_days=DEFAULT_ANALYSIS_PERIOD_DAYS,
    date_from=None, date_to=None, source=SOURCE_BEST_PRICE, search=None, category_id=None,
    machine_id=None, department_id=None, supplier_id=None, preferred_source=None,
    abc_class=None, critical_only=None, stock_status=None, view=None,
    include_inactive=False, include_pricing=False,
):
    """
    The single entry point the route/export layer calls. Returns
    {"rows": [...], "summary": {...}, "methodology": {...}}. `rows` always
    carries the full operational dataset; monetary fields are only
    populated when include_pricing=True (gated by the caller's
    _may_see_pricing(), exactly like every other financial figure in this
    app) — never silently omitted vs. zeroed, so a non-priced viewer sees
    explicit nulls rather than a misleading 0.
    """
    as_of = as_of or business_today()
    if horizon_months is None or horizon_months < 1:
        horizon_months = DEFAULT_HORIZON_MONTHS
    if analysis_period_days is None or analysis_period_days < 1:
        analysis_period_days = DEFAULT_ANALYSIS_PERIOD_DAYS
    if source not in SOURCE_CHOICES:
        source = SOURCE_BEST_PRICE

    parts = sp_service.search_spare_parts(search, include_inactive=include_inactive, category_id=category_id)

    if machine_id:
        allowed = _ids_with_machine(machine_id)
        parts = [p for p in parts if p.id in allowed]
    if department_id:
        allowed = _ids_with_department_history(department_id)
        parts = [p for p in parts if p.id in allowed]
    if supplier_id:
        allowed = _ids_with_supplier(supplier_id)
        parts = [p for p in parts if p.id in allowed]
    if critical_only:
        parts = [p for p in parts if p.critical]
    if preferred_source:
        parts = [p for p in parts if p.preferred_cost_source == preferred_source]

    ids = [p.id for p in parts]
    stocks = movement_svc.bulk_current_stock(ids)
    first_ledger_dates = analytics.bulk_first_ledger_date(ids)
    last_stock_out_dates = analytics.bulk_last_active_stock_out_date(ids)

    # The four fixed trailing columns (30/90/180/365 days) are always
    # anchored to `as_of` (today), regardless of the analysis-period
    # control below — they are absolute recency indicators shown
    # side-by-side in the table, not inputs to the forecast itself.
    trailing = {days: analytics.window_bounds(as_of, days) for days in TRAILING_WINDOWS_DAYS}

    # The "analysis window" drives average_monthly_consumption/trend. A
    # custom date_from/date_to replaces the preset analysis_period_days
    # entirely (its own span becomes the window, anchored at date_to for
    # the recent/preceding trend halves) rather than being layered on top
    # of it.
    if date_from and date_to:
        analysis_bounds = (date_from, date_to)
        analysis_days_effective = (analytics.parse_date(date_to) - analytics.parse_date(date_from)).days + 1
        trend_anchor = date_to
    else:
        analysis_bounds = analytics.window_bounds(as_of, analysis_period_days)
        analysis_days_effective = analysis_period_days
        trend_anchor = as_of
    trailing["analysis"] = analysis_bounds
    half_days = max(1, analysis_days_effective // 2)
    trailing["recent_half"] = analytics.window_bounds(trend_anchor, half_days)
    trailing["preceding_half"] = analytics.preceding_window_bounds(trend_anchor, half_days)

    consumption = {
        key: analytics.bulk_consumption(ids, bounds[0], bounds[1]) for key, bounds in trailing.items()
    }

    category_names = {c.id: c.name for c in SparePartCategory.query.all()}
    department_names = {d.id: d.name for d in SparePartDepartment.query.all()}
    machine_names = {m.id: m.name for m in Machine.query.all()}
    part_machines = {}
    for spare_part_id, machine_id_ in db.session.query(SparePartMachine.spare_part_id, SparePartMachine.machine_id).filter(
        SparePartMachine.spare_part_id.in_(ids)
    ).all():
        part_machines.setdefault(spare_part_id, []).append(machine_names.get(machine_id_, ""))

    # Departments a part has actually been ISSUED to (active Stock Out
    # attribution), not a master-level association — SparePart has no such
    # concept, only SparePartMovement.department_id does (see its
    # docstring). Mirrors part_machines above: distinct names, one query.
    part_departments = {}
    dept_rows = (
        db.session.query(SparePartMovement.spare_part_id, SparePartMovement.department_id)
        .filter(
            SparePartMovement.spare_part_id.in_(ids), SparePartMovement.status == STATUS_ACTIVE,
            SparePartMovement.movement_type == MOVEMENT_STOCK_OUT, SparePartMovement.department_id.isnot(None),
        )
        .distinct()
        .all()
    )
    for spare_part_id, dept_id in dept_rows:
        part_departments.setdefault(spare_part_id, []).append(department_names.get(dept_id, ""))

    rows = []
    for part in parts:
        current_stock = stocks.get(part.id, Decimal("0"))
        history_start = first_ledger_dates.get(part.id)
        if history_start is None:
            history_start = part.created_at.date().isoformat() if part.created_at else as_of
        history_days = analytics.history_days_available(history_start, as_of)
        effective_analysis_window = min(analysis_days_effective, history_days) if history_days else analysis_days_effective

        analysis_total = consumption["analysis"].get(part.id, Decimal("0"))
        avg_monthly = analytics.average_monthly_consumption(analysis_total, effective_analysis_window)

        recent_total = consumption.get("recent_half", {}).get(part.id, Decimal("0"))
        preceding_total = consumption.get("preceding_half", {}).get(part.id, Decimal("0"))
        has_full_preceding = bool(history_days and history_days >= half_days * 2)
        trend = analytics.classify_trend(recent_total, preceding_total, has_full_preceding_window=has_full_preceding)
        recent_monthly = analytics.average_monthly_consumption(recent_total, min(half_days, history_days or half_days))

        forecast = analytics.forecast_monthly_demand(avg_monthly)
        safety_stock = safety_stock_for(part.safety_stock_override, avg_monthly)
        incoming_stock = confirmed_incoming_stock_for(part)
        coverage_months = stock_coverage_months(current_stock, forecast)
        stockout_date = estimated_stockout_date(current_stock, forecast, as_of)
        status = stock_status_for(current_stock, part.minimum_stock)

        last_stock_out = last_stock_out_dates.get(part.id)
        days_since_last_stock_out = None
        if last_stock_out is not None:
            days_since_last_stock_out = (analytics.parse_date(as_of) - analytics.parse_date(last_stock_out)).days
        is_dead_stock, is_slow_moving = classify_movement_speed(
            current_stock=current_stock, history_days=history_days,
            days_since_last_stock_out=days_since_last_stock_out, coverage_months=coverage_months,
        )

        horizon_purchases = {
            months: _quantize_qty(recommended_purchase(forecast * months, safety_stock, current_stock, incoming_stock))
            for months in HORIZON_CHOICES
        }
        selected_purchase = _quantize_qty(
            recommended_purchase(forecast * horizon_months, safety_stock, current_stock, incoming_stock)
        )

        china_price = part.china_buying_price if _usable(part.china_buying_price) else None
        local_price = part.local_buying_price if _usable(part.local_buying_price) else None
        china_cost = procurement_cost(selected_purchase, china_price)
        local_cost = procurement_cost(selected_purchase, local_price)
        if source == SOURCE_CHINA:
            selected_source, selected_unit_price = SOURCE_CHINA, china_price
        elif source == SOURCE_LOCAL:
            selected_source, selected_unit_price = SOURCE_LOCAL, local_price
        else:
            selected_source, selected_unit_price = best_price_selection(china_price, local_price)
        selected_cost = procurement_cost(selected_purchase, selected_unit_price)
        savings = None
        if china_cost is not None and local_cost is not None:
            savings = abs(china_cost - local_cost)

        annual_quantity = avg_monthly * Decimal("12")
        current_cost_value = sp_service.current_cost(part)
        unit_cost = current_cost_value if _usable(current_cost_value) else None

        rows.append({
            "id": part.id, "name": part.name, "specifications": part.specifications,
            "display_name": part.display_name, "code": part.code,
            "category_id": part.category_id, "category_name": category_names.get(part.category_id),
            "machine_names": part_machines.get(part.id, []),
            "department_names": part_departments.get(part.id, []),
            "unit": part.unit, "active": part.active,
            "critical": part.critical, "critical_note": part.critical_note,
            "current_stock": str(_quantize_qty(current_stock)),
            "minimum_stock": str(part.minimum_stock) if part.minimum_stock is not None else None,
            "safety_stock": str(_quantize_qty(safety_stock)),
            "incoming_stock": incoming_stock_display(incoming_stock),
            "safety_stock_override": str(part.safety_stock_override) if part.safety_stock_override is not None else None,
            "quantity_used_30d": str(_quantize_qty(consumption.get(30, {}).get(part.id, Decimal("0")))),
            "quantity_used_90d": str(_quantize_qty(consumption.get(90, {}).get(part.id, Decimal("0")))),
            "quantity_used_180d": str(_quantize_qty(consumption.get(180, {}).get(part.id, Decimal("0")))),
            "quantity_used_365d": str(_quantize_qty(consumption.get(365, {}).get(part.id, Decimal("0")))),
            "average_monthly_consumption": str(_quantize_qty(avg_monthly)),
            "recent_monthly_consumption": str(_quantize_qty(recent_monthly)),
            "consumption_trend": trend.label,
            "stock_coverage_months": str(coverage_months.quantize(Decimal("0.1"))) if coverage_months is not None else None,
            "estimated_stockout_date": stockout_date,
            "recommended_purchase_3m": str(horizon_purchases[3]),
            "recommended_purchase_6m": str(horizon_purchases[6]),
            "recommended_purchase_9m": str(horizon_purchases[9]),
            "recommended_purchase_12m": str(horizon_purchases[12]),
            "recommended_purchase_selected": str(selected_purchase),
            "selected_horizon_months": horizon_months,
            "preferred_cost_source": part.preferred_cost_source,
            "selected_source": selected_source,
            "stock_status": status,
            "is_slow_moving": is_slow_moving,
            "is_dead_stock": is_dead_stock,
            "_annual_quantity": annual_quantity,  # internal, used for ABC; stripped before return
            "_unit_cost": unit_cost,
        })
        if include_pricing:
            rows[-1].update({
                "china_buying_price": str(_quantize_money(china_price)) if china_price is not None else None,
                "local_buying_price": str(_quantize_money(local_price)) if local_price is not None else None,
                "china_projected_cost": str(_quantize_money(china_cost)) if china_cost is not None else None,
                "local_projected_cost": str(_quantize_money(local_cost)) if local_cost is not None else None,
                "selected_source_projected_cost": str(_quantize_money(selected_cost)) if selected_cost is not None else None,
                "estimated_savings": str(_quantize_money(savings)) if savings is not None else None,
            })

    abc_result = classify_abc([{"id": r["id"], "annual_quantity": r["_annual_quantity"], "unit_cost": r["_unit_cost"]} for r in rows])
    for r in rows:
        r["abc_class"] = abc_result.class_by_id.get(r["id"], ABC_UNCLASSIFIED)
        r["procurement_priority"] = procurement_priority(
            stock_status=r["stock_status"],
            coverage_months=Decimal(r["stock_coverage_months"]) if r["stock_coverage_months"] else None,
            critical=r["critical"], trend_label=r["consumption_trend"],
        )
        del r["_annual_quantity"]
        del r["_unit_cost"]

    if abc_class:
        rows = [r for r in rows if r["abc_class"] == abc_class]
    if stock_status:
        rows = [r for r in rows if r["stock_status"] == stock_status]
    rows = _apply_view(rows, view)

    summary = _build_summary(rows, include_pricing=include_pricing)
    return {
        "rows": rows,
        "summary": summary,
        "methodology": {
            "abc_methodology": abc_result.methodology,
            "abc_price_coverage": str(abc_result.price_coverage_ratio.quantize(Decimal("0.01"))),
            "as_of": as_of,
            "analysis_period_days": analysis_period_days,
            "horizon_months": horizon_months,
            "source": source,
            "incoming_stock_tracked": INCOMING_STOCK_TRACKED,
        },
    }


def _apply_view(rows, view):
    if not view or view not in VIEWS:
        return sorted(rows, key=lambda r: (_priority_rank(r["procurement_priority"]), -Decimal(r["recommended_purchase_selected"])))
    if view == VIEW_MOST_CONSUMED:
        return sorted(rows, key=lambda r: Decimal(r["quantity_used_365d"]), reverse=True)
    if view == VIEW_FASTEST_MOVING:
        return sorted(rows, key=lambda r: Decimal(r["recent_monthly_consumption"]), reverse=True)
    if view == VIEW_INCREASING:
        return [r for r in rows if r["consumption_trend"] == "Increasing"]
    if view == VIEW_STOCKOUT_RISK:
        return sorted(
            [r for r in rows if r["stock_status"] == STATUS_STOCKOUT or (r["stock_coverage_months"] and Decimal(r["stock_coverage_months"]) <= URGENT_COVERAGE_MONTHS)],
            key=lambda r: Decimal(r["stock_coverage_months"]) if r["stock_coverage_months"] else Decimal("-1"),
        )
    if view == VIEW_BELOW_MINIMUM:
        return [r for r in rows if r["stock_status"] == STATUS_BELOW_MINIMUM]
    if view == VIEW_CRITICAL:
        return [r for r in rows if r["critical"]]
    if view == VIEW_HIGHEST_VALUE:
        return sorted(rows, key=lambda r: Decimal(r.get("selected_source_projected_cost") or "0"), reverse=True)
    if view == VIEW_CHINA_SAVINGS:
        # r.get("estimated_savings") alone would be a string-truthiness
        # check, which treats "0.00" (zero savings -- both sources cost
        # the same) as truthy and wrongly lets it through. Only a
        # genuinely positive, parseable savings figure qualifies.
        savers = [r for r in rows if r.get("estimated_savings") and Decimal(r["estimated_savings"]) > 0]
        return sorted(savers, key=lambda r: Decimal(r["estimated_savings"]), reverse=True)
    if view == VIEW_SLOW_MOVING:
        return [r for r in rows if r["is_slow_moving"]]
    if view == VIEW_DEAD_STOCK:
        return [r for r in rows if r["is_dead_stock"]]
    return rows


def _priority_rank(priority):
    return {PRIORITY_URGENT: 0, PRIORITY_HIGH: 1, PRIORITY_MEDIUM: 2, PRIORITY_LOW: 3}.get(priority, 4)


def _build_summary(rows, *, include_pricing):
    def total(field):
        return sum((Decimal(r[field]) for r in rows), Decimal("0"))

    summary = {
        "active_parts_count": len(rows),
        "total_quantity_issued": str(total("quantity_used_365d")),
        "fast_moving_count": sum(1 for r in rows if r["abc_class"] == "A"),
        "stockout_risk_count": sum(
            1 for r in rows
            if r["stock_status"] == STATUS_STOCKOUT or (r["stock_coverage_months"] and Decimal(r["stock_coverage_months"]) <= URGENT_COVERAGE_MONTHS)
        ),
        "below_minimum_count": sum(1 for r in rows if r["stock_status"] == STATUS_BELOW_MINIMUM),
        "critical_at_risk_count": sum(1 for r in rows if r["critical"] and r["procurement_priority"] in (PRIORITY_URGENT, PRIORITY_HIGH)),
        "slow_moving_count": sum(1 for r in rows if r["is_slow_moving"]),
        "dead_stock_count": sum(1 for r in rows if r["is_dead_stock"]),
        "procurement_value_3m": None, "procurement_value_6m": None, "procurement_value_12m": None,
        "procurement_value_selected": None, "china_sourcing_total": None, "local_sourcing_total": None,
        "potential_savings": None,
    }
    if include_pricing:
        def money_total(field):
            values = [Decimal(r[field]) for r in rows if r.get(field) is not None]
            return str(sum(values, Decimal("0")).quantize(Decimal("0.01"))) if values else None

        def horizon_value(months_field):
            values = []
            for r in rows:
                price = Decimal(r["china_buying_price"]) if r.get("china_buying_price") else (
                    Decimal(r["local_buying_price"]) if r.get("local_buying_price") else None
                )
                if price is None:
                    continue
                values.append(Decimal(r[months_field]) * price)
            return str(sum(values, Decimal("0")).quantize(Decimal("0.01"))) if values else None

        summary["procurement_value_3m"] = horizon_value("recommended_purchase_3m")
        summary["procurement_value_6m"] = horizon_value("recommended_purchase_6m")
        summary["procurement_value_12m"] = horizon_value("recommended_purchase_12m")
        summary["procurement_value_selected"] = money_total("selected_source_projected_cost")
        summary["china_sourcing_total"] = money_total("china_projected_cost")
        summary["local_sourcing_total"] = money_total("local_projected_cost")
        summary["potential_savings"] = money_total("estimated_savings")
    return summary
