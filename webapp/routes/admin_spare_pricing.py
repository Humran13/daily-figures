"""
Admin > Spare Parts > Pricing & Profit. Super Administrator only, enforced
here on every route (never only in the page). Figures are computed by
spare_part_service.pricing_summary(); price changes are written through
record_price_change(), which keeps the history in audit_log.
"""
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request

from webapp.auth import current_user, roles_required
from webapp.extensions import db
from webapp.models.spare_part import SparePart
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services import spare_part_movement_service as movement_svc
from webapp.services import spare_part_service as svc
from webapp.services.audit_service import record_audit

admin_spare_pricing_bp = Blueprint(
    "admin_spare_pricing", __name__, url_prefix="/api/admin/spare-parts/pricing",
)

TOTAL_KEYS = (
    "stock_value_at_buying", "expected_sales_value", "expected_gross_profit",
)


def _serialize(summary):
    return {key: (str(value) if isinstance(value, Decimal) else value) for key, value in summary.items()}


def _row(spare_part):
    stock = movement_svc.current_stock(spare_part.id)
    summary = svc.pricing_summary(spare_part, stock)
    return {
        "id": spare_part.id,
        "name": spare_part.name,
        "specifications": spare_part.specifications,
        "unit": spare_part.unit,
        "active": spare_part.active,
        **_serialize(summary),
    }


def _totals(rows):
    totals = {}
    for key in TOTAL_KEYS:
        values = [Decimal(r[key]) for r in rows if r[key] is not None]
        totals[key] = str(sum(values, Decimal("0")).quantize(Decimal("0.01"))) if values else None
    totals["priced_count"] = sum(1 for r in rows if r["buying_price"] is not None and r["selling_price"] is not None)
    totals["unpriced_count"] = len(rows) - totals["priced_count"]
    return totals


def _parse_price(value, field):
    if value in (None, ""):
        return None
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        raise svc.SparePartError(f"{field} must be a number") from None
    if price < 0:
        raise svc.SparePartError(f"{field} cannot be negative")
    return price


@admin_spare_pricing_bp.route("", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN)
def list_pricing():
    include_inactive = request.args.get("include_inactive") == "1"
    parts = svc.search_spare_parts(request.args.get("q"), include_inactive=include_inactive)
    rows = [_row(p) for p in parts]
    return jsonify({"rows": rows, "totals": _totals(rows)})


@admin_spare_pricing_bp.route("/<int:spare_part_id>", methods=["PATCH"])
@roles_required(ROLE_SUPER_ADMIN)
def update_pricing(spare_part_id):
    spare_part = db.session.get(SparePart, spare_part_id)
    if spare_part is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    before_buying, before_selling = spare_part.buying_price, spare_part.selling_price
    try:
        if "buying_price" in d:
            spare_part.buying_price = _parse_price(d.get("buying_price"), "buying_price")
        if "selling_price" in d:
            spare_part.selling_price = _parse_price(d.get("selling_price"), "selling_price")
    except svc.SparePartError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    db.session.flush()
    svc.record_price_change(spare_part, before_buying, before_selling, current_user())
    db.session.commit()
    return jsonify(_row(spare_part))


@admin_spare_pricing_bp.route("/<int:spare_part_id>/history", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN)
def price_history(spare_part_id):
    if db.session.get(SparePart, spare_part_id) is None:
        return jsonify({"error": "not found"}), 404
    return jsonify([row.to_dict() for row in svc.price_history(spare_part_id)])
