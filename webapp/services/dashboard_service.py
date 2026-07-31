"""
Dashboard aggregation — reuses webapp/services/stock_service.py for every
stock number (Opening/Production/Returns/Issued/Closing Stock) rather than
recomputing any of it here; this module only counts/groups records and
shapes the response. No monetary figures are computed or invented anywhere
in this app, so none appear here either.
"""
from datetime import datetime, timedelta, timezone

from webapp.extensions import db
from webapp.models.customer import Customer
from webapp.models.daily_figure import StockAdjustment
from webapp.models.dispatch import SHIFT_DAY, SHIFT_NIGHT, STATUS_DRAFT, STATUS_FINALIZED, STATUS_VOID, Dispatch, DispatchLine
from webapp.models.product import Product
from webapp.models.production_record import ProductionRecord
from webapp.models.production_record import STATUS_DRAFT as PRODUCTION_STATUS_DRAFT
from webapp.models.production_record import STATUS_FINALIZED as PRODUCTION_STATUS_FINALIZED
from webapp.models.return_record import ReturnRecord
from webapp.models.return_record import STATUS_DRAFT as RETURN_STATUS_DRAFT
from webapp.models.return_record import STATUS_FINALIZED as RETURN_STATUS_FINALIZED
from webapp.services import stock_service as svc

RECENT_WINDOW_DAYS = 7


def _days_ago(date_str, n):
    d = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=n)
    return d.strftime("%Y-%m-%d")


def _utcnow_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _last_activity_for_date(date):
    """Latest timestamp among any Dispatch/Return/Production record
    (draft or finalized) whose own `date` field is this date — a factual
    "when was this day last touched" marker, not a live-streaming clock."""
    candidates = []
    d = db.session.query(db.func.max(Dispatch.updated_at)).filter(Dispatch.date == date).scalar()
    if d:
        candidates.append(d)
    r = db.session.query(db.func.max(ReturnRecord.updated_at)).filter(ReturnRecord.date == date).scalar()
    if r:
        candidates.append(r)
    p = db.session.query(db.func.max(ProductionRecord.updated_at)).filter(ProductionRecord.date == date).scalar()
    if p:
        candidates.append(p)
    return max(candidates).isoformat() if candidates else None


def _activity_counts(date):
    dispatch_finalized = Dispatch.query.filter_by(date=date, status=STATUS_FINALIZED).count()
    dispatch_draft = Dispatch.query.filter_by(date=date, status=STATUS_DRAFT).count()
    return_finalized = ReturnRecord.query.filter_by(date=date, status=RETURN_STATUS_FINALIZED).count()
    return_draft = ReturnRecord.query.filter_by(date=date, status=RETURN_STATUS_DRAFT).count()
    production_day_finalized = ProductionRecord.query.filter_by(
        date=date, shift=SHIFT_DAY, status=PRODUCTION_STATUS_FINALIZED).count()
    production_night_finalized = ProductionRecord.query.filter_by(
        date=date, shift=SHIFT_NIGHT, status=PRODUCTION_STATUS_FINALIZED).count()
    production_draft = ProductionRecord.query.filter_by(date=date, status=PRODUCTION_STATUS_DRAFT).count()

    return {
        "dispatch": {"finalized": dispatch_finalized, "draft": dispatch_draft},
        "returns": {"finalized": return_finalized, "draft": return_draft},
        "production": {
            "day_finalized": production_day_finalized,
            "night_finalized": production_night_finalized,
            "draft": production_draft,
        },
        "last_activity_at": _last_activity_for_date(date),
    }


def _attention_notices(date, activity, stock_summary):
    """Neutral, factual notices derived from data that already has a
    defined expectation for the day — never a vague "something's missing"."""
    notices = []
    if activity["dispatch"]["finalized"] == 0:
        notices.append({"type": "no_finalized_dispatch", "message": "No finalized Dispatch records for the selected date."})
    if activity["returns"]["finalized"] == 0:
        notices.append({"type": "no_finalized_returns", "message": "No finalized Return records for the selected date."})
    if activity["production"]["day_finalized"] == 0:
        notices.append({"type": "no_finalized_day_production", "message": "No finalized Day Production records for the selected date."})
    if activity["production"]["night_finalized"] == 0:
        notices.append({"type": "no_finalized_night_production", "message": "No finalized Night Production records for the selected date."})

    total_drafts = activity["dispatch"]["draft"] + activity["returns"]["draft"] + activity["production"]["draft"]
    if total_drafts > 0:
        notices.append({
            "type": "drafts_pending", "count": total_drafts,
            "message": f"{total_drafts} draft record{'s' if total_drafts != 1 else ''} awaiting finalization.",
        })

    for row in stock_summary:
        if row["closing_base_qty"] < 0:
            notices.append({
                "type": "negative_closing_stock", "product_id": row["product_id"], "product_name": row["product_name"],
                "message": f"{row['product_name']} has a negative calculated Closing Stock.",
            })

    return notices


def _production_by_shift(stock_summary, date):
    """Day/Night Production, per product, alongside the (shift-agnostic)
    combined figure already in stock_summary — reuses the exact same
    finalized-Production-Book aggregation stock_service.py uses for Daily
    Figures itself, never a second calculation."""
    result = {}
    for row in stock_summary:
        pid = row["product_id"]
        result[pid] = {
            "day": svc.production_finalized_base_qty(pid, date, SHIFT_DAY),
            "night": svc.production_finalized_base_qty(pid, date, SHIFT_NIGHT),
        }
    return result


def build_dashboard(date):
    stock_summary = svc.date_range_summary(date, date)

    thresholds = {
        p.id: p.low_stock_threshold
        for p in Product.query.filter(Product.id.in_([row["product_id"] for row in stock_summary])).all()
    }
    low_stock = [
        row for row in stock_summary
        if thresholds.get(row["product_id"]) is not None
        and row["closing_base_qty"] <= thresholds[row["product_id"]]
    ]

    activity = _activity_counts(date)
    production_by_shift = _production_by_shift(stock_summary, date)
    attention = _attention_notices(date, activity, stock_summary)

    recent_dispatches = (
        Dispatch.query.filter_by(date=date).order_by(Dispatch.created_at.desc()).limit(8).all()
    )

    window_start = _days_ago(date, RECENT_WINDOW_DAYS - 1)
    top_products_rows = (
        db.session.query(
            DispatchLine.product_id,
            db.func.sum(DispatchLine.base_unit_qty).label("total"),
        )
        .join(Dispatch, Dispatch.id == DispatchLine.dispatch_id)
        .filter(
            Dispatch.status == STATUS_FINALIZED,
            Dispatch.date >= window_start,
            Dispatch.date <= date,
        )
        .group_by(DispatchLine.product_id)
        .order_by(db.desc("total"))
        .limit(5)
        .all()
    )
    products_by_id = {p.id: p for p in Product.query.filter(
        Product.id.in_([r.product_id for r in top_products_rows])
    ).all()}
    top_products = []
    for r in top_products_rows:
        product = products_by_id.get(r.product_id)
        if product is None:
            continue
        rule = product.current_packaging_rule()
        entry = {"product_id": r.product_id, "product_name": product.name, "base_qty": int(r.total),
                 "packaging_rule": rule.to_dict() if rule else None}
        if rule:
            cartons, packs, pieces = svc.from_base_units(int(r.total), rule)
            entry.update({"cartons": cartons, "packs": packs, "pieces": pieces})
        top_products.append(entry)

    active_customers = Customer.query.filter_by(active=True).count()

    draft_dispatches = (
        Dispatch.query.filter_by(status=STATUS_DRAFT).order_by(Dispatch.created_at.desc()).limit(10).all()
    )

    recent_adjustments = StockAdjustment.query.order_by(StockAdjustment.created_at.desc()).limit(8).all()
    recent_voids = (
        Dispatch.query.filter_by(status=STATUS_VOID).order_by(Dispatch.voided_at.desc()).limit(5).all()
    )

    return {
        "date": date,
        "generated_at": _utcnow_iso(),
        "activity": activity,
        "stock_summary": stock_summary,
        "production_by_shift": production_by_shift,
        "attention": attention,
        "low_stock": low_stock,
        "recent_dispatches": [d.to_dict(include_lines=False) for d in recent_dispatches],
        "top_products": top_products,
        "top_products_window_days": RECENT_WINDOW_DAYS,
        "active_customers": active_customers,
        "draft_dispatches": {
            "count": len(draft_dispatches),
            "items": [d.to_dict(include_lines=False) for d in draft_dispatches],
        },
        "recent_adjustments": [a.to_dict() for a in recent_adjustments],
        "recent_voids": [d.to_dict(include_lines=False) for d in recent_voids],
    }
