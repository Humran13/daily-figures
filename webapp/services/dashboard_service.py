from datetime import datetime, timedelta

from webapp.extensions import db
from webapp.models.customer import Customer
from webapp.models.daily_figure import StockAdjustment
from webapp.models.dispatch import STATUS_DRAFT, STATUS_FINALIZED, STATUS_VOID, Dispatch, DispatchLine
from webapp.models.product import Product
from webapp.services import stock_service as svc

RECENT_WINDOW_DAYS = 7


def _days_ago(date_str, n):
    d = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=n)
    return d.strftime("%Y-%m-%d")


def build_dashboard(today):
    stock_summary = svc.date_range_summary(today, today)

    thresholds = {
        p.id: p.low_stock_threshold
        for p in Product.query.filter(Product.id.in_([row["product_id"] for row in stock_summary])).all()
    }
    low_stock = [
        row for row in stock_summary
        if thresholds.get(row["product_id"]) is not None
        and row["closing_base_qty"] <= thresholds[row["product_id"]]
    ]

    recent_dispatches = (
        Dispatch.query.order_by(Dispatch.created_at.desc()).limit(8).all()
    )

    window_start = _days_ago(today, RECENT_WINDOW_DAYS - 1)
    top_products_rows = (
        db.session.query(
            DispatchLine.product_id,
            db.func.sum(DispatchLine.base_unit_qty).label("total"),
        )
        .join(Dispatch, Dispatch.id == DispatchLine.dispatch_id)
        .filter(
            Dispatch.status == STATUS_FINALIZED,
            Dispatch.date >= window_start,
            Dispatch.date <= today,
        )
        .group_by(DispatchLine.product_id)
        .order_by(db.desc("total"))
        .limit(5)
        .all()
    )
    products_by_id = {p.id: p for p in Product.query.filter(
        Product.id.in_([r.product_id for r in top_products_rows])
    ).all()}
    top_products = [
        {"product_id": r.product_id, "product_name": products_by_id[r.product_id].name, "base_qty": int(r.total)}
        for r in top_products_rows if r.product_id in products_by_id
    ]

    active_customers = Customer.query.filter_by(active=True).count()

    draft_dispatches = (
        Dispatch.query.filter_by(status=STATUS_DRAFT).order_by(Dispatch.created_at.desc()).limit(10).all()
    )

    recent_adjustments = StockAdjustment.query.order_by(StockAdjustment.created_at.desc()).limit(8).all()
    recent_voids = (
        Dispatch.query.filter_by(status=STATUS_VOID).order_by(Dispatch.voided_at.desc()).limit(5).all()
    )

    return {
        "date": today,
        "stock_summary": stock_summary,
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
