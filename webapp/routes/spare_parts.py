import logging
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request
from sqlalchemy.exc import SQLAlchemyError

from webapp.auth import current_user, login_required, roles_required
from webapp.extensions import db
from webapp.models.spare_part import SparePart
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import spare_part_movement_service as movement_svc
from webapp.services import spare_part_service as svc
from webapp.services.audit_service import record_audit

logger = logging.getLogger(__name__)

spare_parts_bp = Blueprint("spare_parts", __name__, url_prefix="/api/spare-parts")

# Buying/Selling prices and profit are financial data: Super Administrator
# only. Operational roles (including Manager) never receive them, and can't
# write them either — ongoing pricing is managed from Admin > Spare Parts >
# Pricing & Profit (see webapp/routes/admin_spare_pricing.py).
PRICING_ROLES = (ROLE_SUPER_ADMIN,)
PRICE_FIELDS = ("buying_price", "selling_price")


def _may_see_pricing():
    user = current_user()
    return user is not None and user.role in PRICING_ROLES


def _to_dict(spare_part):
    stock = movement_svc.current_stock(spare_part.id)
    d = spare_part.to_dict(current_stock=stock, include_pricing=_may_see_pricing())
    d["machines"] = [m.to_dict() for m in svc.spare_part_machines(spare_part.id)]
    d["low_stock"] = movement_svc.is_low_stock(spare_part, stock)
    return d


def _parse_decimal(value, field):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise svc.SparePartError(f"{field} must be a number") from None


def _pricing_forbidden_response(payload):
    """Returns a 403 response when a non-Super-Admin tries to send any price field."""
    if any(field in payload for field in PRICE_FIELDS) and not _may_see_pricing():
        return jsonify({"error": "Only a Super Administrator can set buying or selling prices."}), 403
    return None


@spare_parts_bp.route("", methods=["GET"])
@login_required
def list_spare_parts():
    category_id = request.args.get("category_id")
    include_inactive = request.args.get("include_inactive") == "1"
    category = None
    if category_id:
        try:
            category = int(category_id)
        except ValueError:
            return jsonify({"error": "invalid category_id"}), 400
    parts = svc.search_spare_parts(
        request.args.get("q"), include_inactive=include_inactive, category_id=category,
    )
    return jsonify([_to_dict(p) for p in parts])


@spare_parts_bp.route("/<int:spare_part_id>", methods=["GET"])
@login_required
def get_spare_part(spare_part_id):
    spare_part = db.session.get(SparePart, spare_part_id)
    if spare_part is None:
        return jsonify({"error": "not found"}), 404
    d = _to_dict(spare_part)
    d["suppliers"] = [s.to_dict() for s in svc.spare_part_suppliers(spare_part_id)]
    return jsonify(d)


@spare_parts_bp.route("", methods=["POST"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def create_spare_part():
    d = request.get_json(force=True) or {}
    forbidden = _pricing_forbidden_response(d)
    if forbidden:
        return forbidden
    try:
        minimum_stock = _parse_decimal(d.get("minimum_stock"), "minimum_stock")
        buying_price = _parse_decimal(d.get("buying_price"), "buying_price")
        selling_price = _parse_decimal(d.get("selling_price"), "selling_price")

        if not d.get("confirm_duplicate"):
            duplicate = svc.find_probable_duplicate(
                d.get("name"), d.get("specifications"), category_id=d.get("category_id"),
            )
            if duplicate is not None:
                return jsonify({"error": "possible_duplicate", "existing": _to_dict(duplicate)}), 409

        spare_part = svc.create_spare_part(
            name=d.get("name"), model=d.get("model"), size=d.get("size"),
            specifications=d.get("specifications"), category_id=d.get("category_id"),
            unit=d.get("unit") or "pcs", minimum_stock=minimum_stock,
            buying_price=buying_price, selling_price=selling_price,
            location=d.get("location"), notes=d.get("notes"),
            machine_ids=d.get("machine_ids"),
        )
    except svc.SparePartError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "create", "spare_part", entity_id=spare_part.id,
                 after=spare_part.to_dict(include_pricing=True))
    db.session.commit()
    return jsonify(_to_dict(spare_part)), 201


@spare_parts_bp.route("/<int:spare_part_id>", methods=["PATCH"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def update_spare_part(spare_part_id):
    spare_part = db.session.get(SparePart, spare_part_id)
    if spare_part is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    forbidden = _pricing_forbidden_response(d)
    if forbidden:
        return forbidden
    before = spare_part.to_dict(include_pricing=True)
    before_buying, before_selling = spare_part.buying_price, spare_part.selling_price
    try:
        if "minimum_stock" in d:
            d["minimum_stock"] = _parse_decimal(d.get("minimum_stock"), "minimum_stock")
        if "buying_price" in d:
            d["buying_price"] = _parse_decimal(d.get("buying_price"), "buying_price")
        if "selling_price" in d:
            d["selling_price"] = _parse_decimal(d.get("selling_price"), "selling_price")
        svc.update_spare_part(spare_part, d, machine_ids=d.get("machine_ids"))
    except svc.SparePartError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "update", "spare_part", entity_id=spare_part.id, before=before,
                 after=spare_part.to_dict(include_pricing=True))
    svc.record_price_change(spare_part, before_buying, before_selling, current_user())
    db.session.commit()
    return jsonify(_to_dict(spare_part))


@spare_parts_bp.route("/<int:spare_part_id>/deletion-preview", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN)
def deletion_preview(spare_part_id):
    spare_part = db.session.get(SparePart, spare_part_id)
    if spare_part is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"id": spare_part.id, "name": spare_part.name,
                    "related": svc.spare_part_deletion_counts(spare_part_id)})


@spare_parts_bp.route("/<int:spare_part_id>", methods=["DELETE"])
@roles_required(ROLE_SUPER_ADMIN)
def delete_spare_part(spare_part_id):
    """
    Permanent delete, Super Administrator only. Removes the spare part with
    its movements and machine/supplier links in ONE transaction; any failure
    rolls the whole thing back so nothing is left half-deleted.
    """
    spare_part = db.session.get(SparePart, spare_part_id)
    if spare_part is None:
        return jsonify({"error": "not found"}), 404
    try:
        removed = svc.permanently_delete_spare_part(spare_part, current_user())
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("permanent delete of spare part %s failed", spare_part_id)
        return jsonify({"error": "Delete failed. Nothing was removed; please try again."}), 500
    return jsonify({"ok": True, "removed": removed})


@spare_parts_bp.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    return jsonify(movement_svc.dashboard_summary())
