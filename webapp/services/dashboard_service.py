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
from webapp.models.daily_figure import OPENING_STOCK_SOURCE_MANUAL_CORRECTION, DailyFigure, StockAdjustment
from webapp.models.daily_entry_status import COMPLETION_TYPE_NO_ACTIVITY, COMPLETION_TYPE_WITH_DATA, DailyEntryStatus
from webapp.models.daily_review_session import REVIEW_PRODUCT_STATE_EDITED, REVIEW_PRODUCT_STATES_COUNTED_AS_REVIEWED, DailyReviewProductState
from webapp.models.dispatch import SHIFT_DAY, SHIFT_NIGHT, STATUS_DRAFT, STATUS_FINALIZED, STATUS_VOID, Dispatch, DispatchLine
from webapp.models.product import Product
from webapp.models.production_record import ProductionRecord
from webapp.models.production_record import STATUS_DRAFT as PRODUCTION_STATUS_DRAFT
from webapp.models.production_record import STATUS_FINALIZED as PRODUCTION_STATUS_FINALIZED
from webapp.models.return_record import ReturnRecord
from webapp.models.return_record import STATUS_DRAFT as RETURN_STATUS_DRAFT
from webapp.models.return_record import STATUS_FINALIZED as RETURN_STATUS_FINALIZED
from webapp.services import daily_review_service, product_usage_service, stock_service as svc
from webapp.services.quantity_format import qty_label

RECENT_WINDOW_DAYS = 7


def _days_ago(date_str, n):
    d = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=n)
    return d.strftime("%Y-%m-%d")


def _utcnow_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _user_label(user_id):
    """Mirrors the same small per-file helper already established in
    stock_service.py/daily_entry_status_service.py/daily_review_service.py
    — duplicated deliberately rather than shared for four lines."""
    if user_id is None:
        return None
    from webapp.models.user import User
    user = db.session.get(User, user_id)
    return user.username if user else "a former user"


def _drafts_view(drafts):
    """Dashboard UX correction — the compact 'Unfinalized Drafts' preview
    wants a 'created by' attribution alongside each draft, which plain
    Dispatch.to_dict() doesn't carry (created_by is only a user id there).
    Dashboard-only enrichment, one batched user query regardless of how
    many drafts are shown — never touches Dispatch.to_dict() itself or
    any other consumer of it."""
    user_ids = {d.created_by for d in drafts if d.created_by is not None}
    from webapp.models.user import User
    users_by_id = {u.id: u.username for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}

    rows = []
    for d in drafts:
        row = d.to_dict(include_lines=False)
        row["created_by_username"] = users_by_id.get(d.created_by, "a former user" if d.created_by else None)
        row["source_type"] = "Dispatch"
        rows.append(row)
    return rows


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


def _adjustments_view(adjustments):
    """Dashboard-only enrichment of StockAdjustment.to_dict() (never
    changes the model's own to_dict() or the general
    GET /api/daily-figures/adjustments endpoint — those are unaffected)
    — adds product_name and a signed, packaging-aware quantity_label
    (e.g. "+2 Ctns" / "-1.05 Ctns") so 'Recent corrections' never shows a
    raw, unlabeled base-unit delta. One batched product query regardless
    of how many adjustments are shown, never one per row."""
    product_ids = {a.product_id for a in adjustments}
    products_by_id = {p.id: p for p in Product.query.filter(Product.id.in_(product_ids)).all()} if product_ids else {}

    rows = []
    for a in adjustments:
        d = a.to_dict()
        product = products_by_id.get(a.product_id)
        d["product_name"] = product.name if product else None
        d["created_by_username"] = _user_label(a.created_by)
        rule = product.current_packaging_rule() if product else None
        if rule:
            sign = "+" if a.delta_base_qty >= 0 else "-"
            cartons, packs, pieces = svc.from_base_units(abs(a.delta_base_qty), rule)
            d["quantity_label"] = f"{sign}{qty_label(cartons, packs, pieces, rule)}"
        else:
            d["quantity_label"] = f"{a.delta_base_qty:+d} pc"
        rows.append(d)
    return rows


def _voids_view(voids):
    """Dashboard-only enrichment of Dispatch.to_dict() for 'Recent
    corrections' — adds voided_by_username so the preview can show 'Voided
    by X' without exposing the numeric user id. One batched user query
    regardless of how many voided dispatches are shown."""
    user_ids = {d.voided_by for d in voids if d.voided_by is not None}
    from webapp.models.user import User
    users_by_id = {u.id: u.username for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}

    rows = []
    for d in voids:
        row = d.to_dict(include_lines=False)
        row["voided_by_username"] = users_by_id.get(d.voided_by, "a former user" if d.voided_by else None)
        rows.append(row)
    return rows


def _today_status(product_id, entry_status_by_shift, review_states_by_shift):
    """A short, useful label for what happened to this product today —
    priority: an explicit review action outranks a plain completion,
    which outranks unreviewed raw activity. Never itself a source of
    truth (see _products_worked_on_today() for the actual qualifying
    rule) — purely a display hint for the compact preview row."""
    for shift_states in review_states_by_shift.values():
        state = shift_states.get(product_id)
        if state == REVIEW_PRODUCT_STATE_EDITED:
            return "edited", "Edited"
    for shift_states in review_states_by_shift.values():
        if shift_states.get(product_id) in REVIEW_PRODUCT_STATES_COUNTED_AS_REVIEWED:
            return "reviewed", "Reviewed"
    for shift_status in entry_status_by_shift.values():
        row = shift_status.get(product_id)
        if row and row["completion_type"] == COMPLETION_TYPE_NO_ACTIVITY:
            return "no_activity", "No Activity Confirmed"
    for shift_status in entry_status_by_shift.values():
        row = shift_status.get(product_id)
        if row and row["completion_type"] == COMPLETION_TYPE_WITH_DATA:
            return "completed", "Completed"
    return "activity", "Activity recorded"


def _representative_shift(product_id, entry_status_by_shift, review_states_by_shift):
    """Which shift's live daily_figure_view() best represents a worked
    product that has NO stock_summary row of its own (e.g. an explicit
    'No Activity Today'/review touch with no real movement at all — see
    _daily_figures_today()'s fallback) — prefer whichever shift actually
    carries the qualifying signal, Day before Night when both do (the
    Dashboard has no shift selector, so this only affects which shift's
    view backs a necessarily-all-zero-or-carried row)."""
    for shift in (SHIFT_DAY, SHIFT_NIGHT):
        if product_id in entry_status_by_shift.get(shift, {}):
            return shift
        if review_states_by_shift.get(shift, {}).get(product_id) in REVIEW_PRODUCT_STATES_COUNTED_AS_REVIEWED:
            return shift
    return SHIFT_DAY


def _products_worked_on_today(date, all_product_ids, stock_summary_by_id):
    """Final Dashboard UX correction, section 6 — a product belongs on the
    compact 'Per-Product Daily Figures' preview/full-view ONLY if there is
    genuine activity or review work for it today, never merely because it
    carries a non-zero Opening/Closing Stock balance forward from an
    earlier period. Checked across BOTH shifts of the date (the Dashboard
    has no shift selector — 'today' has always meant the whole calendar
    day here, matching stock_summary's own date_range_summary(date, date)
    scope, unchanged). Every lookup here is a single batched query
    (per shift, where relevant) against every active product id — never
    one query per product. Deliberately scans EVERY active product, not
    just ones already present in stock_summary — a product with an
    explicit 'No Activity Today'/review touch but otherwise zero movement
    never gets a stock_summary row at all (date_range_summary() correctly
    leaves a genuinely all-zero product out of that different-purpose
    list), so it would be silently missed if only stock_summary's own
    product ids were ever considered here.

    Returns (worked_product_ids, today_status_by_product,
    entry_status_by_shift, review_states_by_shift) — the caller reuses the
    last two to pick a representative shift for any worked product with no
    stock_summary row of its own (see _representative_shift())."""
    if not all_product_ids:
        return set(), {}, {}, {}

    worked = set()

    # 1) Production/Returns/Issued genuinely non-zero today — already
    #    computed by stock_summary itself, never recomputed here.
    for pid, row in stock_summary_by_id.items():
        if row["production_base_qty"] or row["return_base_qty"] or row["issued_base_qty"]:
            worked.add(pid)

    # 2) Opening Stock manually corrected today, or notes added/changed
    #    today — checked against TODAY's own DailyFigure row specifically,
    #    never the date-range aggregate (which can't distinguish "today's
    #    row" from a value merely carried forward from an earlier one).
    figures_today = DailyFigure.query.filter(
        DailyFigure.date == date, DailyFigure.product_id.in_(all_product_ids),
    ).all()
    for figure in figures_today:
        if figure.opening_stock_source == OPENING_STOCK_SOURCE_MANUAL_CORRECTION or (figure.notes and figure.notes.strip()):
            worked.add(figure.product_id)

    # 3) Explicit completion (No Activity Today, or a real data save) on
    #    either shift — one batched query per shift (2 total), never one
    #    per product.
    entry_status_by_shift = {}
    for shift in (SHIFT_DAY, SHIFT_NIGHT):
        rows = DailyEntryStatus.query.filter(
            DailyEntryStatus.date == date,
            DailyEntryStatus.shift == shift,
            DailyEntryStatus.product_id.in_(all_product_ids),
            DailyEntryStatus.completed.is_(True),
        ).all()
        shift_map = {r.product_id: {"completion_type": r.completion_type} for r in rows}
        entry_status_by_shift[shift] = shift_map
        worked.update(shift_map.keys())

    # 4) Reviewed or edited during the Daily Figures review, on either
    #    shift — a session lookup plus one batched product-state query per
    #    shift that actually has a session (never per product).
    review_states_by_shift = {}
    for shift in (SHIFT_DAY, SHIFT_NIGHT):
        session = daily_review_service.get_session(date, shift)
        if session is None:
            review_states_by_shift[shift] = {}
            continue
        rows = DailyReviewProductState.query.filter(
            DailyReviewProductState.review_session_id == session.id,
            DailyReviewProductState.product_id.in_(all_product_ids),
        ).all()
        shift_map = {r.product_id: r.state for r in rows}
        review_states_by_shift[shift] = shift_map
        worked.update(pid for pid, state in shift_map.items() if state in REVIEW_PRODUCT_STATES_COUNTED_AS_REVIEWED)

    today_status_by_product = {
        pid: _today_status(pid, entry_status_by_shift, review_states_by_shift) for pid in worked
    }
    return worked, today_status_by_product, entry_status_by_shift, review_states_by_shift


def _daily_figures_today(date, stock_summary):
    """The compact 'Per-Product Daily Figures' list — every active
    product worked on today (section 6), ordered by the existing global
    usage ranking (product_usage_service.ranked_active_products(), reused
    as-is — never a second, competing ranking), with the existing
    deterministic display_order/name ordering already built into that
    ranking serving as the final tie-breaker. The frontend previews the
    first three and reveals the rest in a 'View all' panel from this same
    already-fetched list — no second request.

    A worked product that has no stock_summary row of its own (a real,
    if narrow, case — see _products_worked_on_today()'s docstring) falls
    back to a live daily_figure_view() for a representative shift, using
    the exact same authoritative Daily Figures calculation everywhere else
    in the app, never a duplicated or approximated one."""
    ranked = product_usage_service.ranked_active_products()
    all_product_ids = [p.id for p in ranked]
    stock_summary_by_id = {row["product_id"]: row for row in stock_summary}

    worked_ids, today_status, entry_status_by_shift, review_states_by_shift = _products_worked_on_today(
        date, all_product_ids, stock_summary_by_id
    )
    if not worked_ids:
        return []

    items = []
    for product in ranked:
        if product.id not in worked_ids:
            continue
        row = stock_summary_by_id.get(product.id)
        if row is None:
            shift = _representative_shift(product.id, entry_status_by_shift, review_states_by_shift)
            view = svc.daily_figure_view(product, date, shift)
            row = {
                "product_id": view["product_id"], "product_name": view["product_name"],
                "packaging_rule": view["packaging_rule"],
                "opening": view["opening"], "return_": view["return_"], "production": view["production"],
                "issued": view["issued"], "closing": view["closing"],
                "opening_base_qty": (view["opening"] or {}).get("base_qty", 0),
                "return_base_qty": (view["return_"] or {}).get("base_qty", 0),
                "production_base_qty": (view["production"] or {}).get("base_qty", 0),
                "issued_base_qty": (view["issued"] or {}).get("base_qty", 0),
                "closing_base_qty": (view["closing"] or {}).get("base_qty", 0),
            }
        status_code, status_label = today_status.get(product.id, ("activity", "Activity recorded"))
        items.append({**row, "today_status": status_code, "today_status_label": status_label})
    return items


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

    draft_dispatch_count = Dispatch.query.filter_by(status=STATUS_DRAFT).count()
    draft_dispatches = (
        Dispatch.query.filter_by(status=STATUS_DRAFT).order_by(Dispatch.created_at.desc()).limit(10).all()
    )

    daily_figures_today = _daily_figures_today(date, stock_summary)

    recent_adjustments = StockAdjustment.query.order_by(StockAdjustment.created_at.desc()).limit(8).all()
    recent_voids = (
        Dispatch.query.filter_by(status=STATUS_VOID).order_by(Dispatch.voided_at.desc()).limit(5).all()
    )
    recent_adjustments_view = _adjustments_view(recent_adjustments)

    return {
        "date": date,
        "generated_at": _utcnow_iso(),
        "activity": activity,
        "stock_summary": stock_summary,
        "production_by_shift": production_by_shift,
        "daily_figures_today": daily_figures_today,
        "attention": attention,
        "low_stock": low_stock,
        "recent_dispatches": [d.to_dict(include_lines=False) for d in recent_dispatches],
        "top_products": top_products,
        "top_products_window_days": RECENT_WINDOW_DAYS,
        "active_customers": active_customers,
        "draft_dispatches": {
            "count": draft_dispatch_count,
            "items": _drafts_view(draft_dispatches),
        },
        "recent_adjustments": recent_adjustments_view,
        "recent_voids": _voids_view(recent_voids),
    }
