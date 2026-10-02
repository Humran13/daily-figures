from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request

from webapp.auth import current_user, login_required, roles_required
from webapp.extensions import db
from webapp.models.spare_part import SparePart
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import spare_part_movement_service as movement_svc
from webapp.services import spare_part_service as svc
from webapp.services.audit_service import record_audit

spare_parts_bp = Blueprint("spare_parts", __name__, url_prefix="/api/spare-parts")


def _to_dict(spare_part):
    stock = movement_svc.current_stock(spare_part.id)
    d = spare_part.to_dict(current_stock=stock)
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


@spare_parts_bp.route("", methods=["GET"])
@login_required
def list_spare_parts():
    q = (request.args.get("q") or "").strip().lower()
    include_inactive = request.args.get("include_inactive") == "1"
    query = SparePart.query
    if not include_inactive:
        query = query.filter_by(active=True)
    parts = query.order_by(SparePart.name).all()
    if q:
        parts = [
            p for p in parts
            if q in (p.name or "").lower() or q in (p.model or "").lower() or q in (p.size or "").lower()
            or any(q in m.name.lower() for m in svc.spare_part_machines(p.id))
        ]
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
    try:
        minimum_stock = _parse_decimal(d.get("minimum_stock"), "minimum_stock")
        spare_part = svc.create_spare_part(
            name=d.get("name"), model=d.get("model"), size=d.get("size"), unit=d.get("unit") or "pcs",
            minimum_stock=minimum_stock, location=d.get("location"), notes=d.get("notes"),
            machine_ids=d.get("machine_ids"),
        )
    except svc.SparePartError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "create", "spare_part", entity_id=spare_part.id, after=spare_part.to_dict())
    db.session.commit()
    return jsonify(_to_dict(spare_part)), 201


@spare_parts_bp.route("/<int:spare_part_id>", methods=["PATCH"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def update_spare_part(spare_part_id):
    spare_part = db.session.get(SparePart, spare_part_id)
    if spare_part is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    before = spare_part.to_dict()
    try:
        if "minimum_stock" in d:
            d["minimum_stock"] = _parse_decimal(d.get("minimum_stock"), "minimum_stock")
        svc.update_spare_part(spare_part, d, machine_ids=d.get("machine_ids"))
    except svc.SparePartError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "update", "spare_part", entity_id=spare_part.id, before=before, after=spare_part.to_dict())
    db.session.commit()
    return jsonify(_to_dict(spare_part))


@spare_parts_bp.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    return jsonify(movement_svc.dashboard_summary())
