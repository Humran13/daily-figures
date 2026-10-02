from decimal import Decimal, InvalidOperation

from flask import Blueprint, Response, jsonify, request

from webapp.auth import current_user, login_required, roles_required
from webapp.extensions import db
from webapp.models.spare_part import SparePart
from webapp.models.spare_part_movement import MOVEMENT_TYPES, SparePartMovement
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN
from webapp.services import spare_part_movement_service as svc
from webapp.services.audit_service import record_audit
from webapp.services.export_service import MIME_TYPES, build_export

spare_part_movements_bp = Blueprint("spare_part_movements", __name__, url_prefix="/api/spare-parts/movements")


def _parse_qty(d):
    try:
        qty = Decimal(str(d.get("quantity")))
    except (InvalidOperation, TypeError):
        raise svc.SparePartMovementError("quantity must be a number") from None
    return qty


def _get_spare_part(d):
    spare_part = db.session.get(SparePart, d.get("spare_part_id"))
    if spare_part is None:
        raise svc.SparePartMovementError("unknown spare_part_id")
    return spare_part


@spare_part_movements_bp.route("/stock-in", methods=["POST"])
@roles_required(ROLE_OPERATOR, ROLE_MANAGER, ROLE_SUPER_ADMIN)
def stock_in():
    d = request.get_json(force=True) or {}
    try:
        spare_part = _get_spare_part(d)
        movement = svc.record_stock_in(
            spare_part, _parse_qty(d), current_user(), business_date=d.get("date"),
            supplier_id=d.get("supplier_id"), reference_doc=d.get("reference_doc"), remarks=d.get("remarks"),
        )
    except svc.SparePartMovementError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "stock_in", "spare_part_movement", entity_id=movement.id, after=movement.to_dict())
    db.session.commit()
    return jsonify(movement.to_dict()), 201


@spare_part_movements_bp.route("/stock-out", methods=["POST"])
@roles_required(ROLE_OPERATOR, ROLE_MANAGER, ROLE_SUPER_ADMIN)
def stock_out():
    d = request.get_json(force=True) or {}
    try:
        spare_part = _get_spare_part(d)
        movement = svc.record_stock_out(
            spare_part, _parse_qty(d), current_user(), business_date=d.get("date"),
            requested_by=d.get("requested_by"), approved_by=d.get("approved_by"), remarks=d.get("remarks"),
        )
    except svc.SparePartMovementError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "stock_out", "spare_part_movement", entity_id=movement.id, after=movement.to_dict())
    db.session.commit()
    return jsonify(movement.to_dict()), 201


@spare_part_movements_bp.route("/adjustment", methods=["POST"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def adjustment():
    d = request.get_json(force=True) or {}
    try:
        spare_part = _get_spare_part(d)
        movement = svc.record_adjustment(
            spare_part, _parse_qty(d), d.get("direction"), current_user(),
            business_date=d.get("date"), reason=d.get("reason"), remarks=d.get("remarks"),
        )
    except svc.SparePartMovementError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "adjust", "spare_part_movement", entity_id=movement.id, after=movement.to_dict())
    db.session.commit()
    return jsonify(movement.to_dict()), 201


@spare_part_movements_bp.route("/opening-balance", methods=["POST"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def opening_balance():
    d = request.get_json(force=True) or {}
    try:
        spare_part = _get_spare_part(d)
        movement = svc.record_opening_balance(
            spare_part, _parse_qty(d), current_user(), business_date=d.get("date"), remarks=d.get("remarks"),
        )
    except svc.SparePartMovementError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "adjust", "spare_part_movement", entity_id=movement.id, after=movement.to_dict())
    db.session.commit()
    return jsonify(movement.to_dict()), 201


@spare_part_movements_bp.route("/<int:movement_id>/void", methods=["POST"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def void(movement_id):
    movement = db.session.get(SparePartMovement, movement_id)
    if movement is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    before = movement.to_dict()
    try:
        svc.void_movement(movement, current_user(), d.get("reason"))
    except svc.SparePartMovementError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "void", "spare_part_movement", entity_id=movement.id, before=before, after=movement.to_dict())
    db.session.commit()
    return jsonify(movement.to_dict())


@spare_part_movements_bp.route("", methods=["GET"])
@login_required
def history():
    try:
        _, rows = svc.movement_history_data(request.args)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid filter parameters"}), 400
    return jsonify([m.to_dict() for m in rows])


@spare_part_movements_bp.route("/export.<fmt>", methods=["GET"])
@login_required
def export(fmt):
    if fmt not in MIME_TYPES:
        return jsonify({"error": "unsupported format"}), 400
    try:
        values, rows = svc.movement_history_data(request.args)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid filter parameters"}), 400
    filters = {k: v for k, v in values.items() if v not in (None, "", [])}
    columns = [
        ("business_date", "Date"), ("spare_part_name", "Spare Name"), ("movement_type", "Movement Type"),
        ("quantity", "Quantity"), ("requested_by", "Requested By"), ("approved_by", "Approved By"),
        ("entered_by", "Entered By"), ("remarks", "Remarks"), ("status", "Status"),
    ]
    spare_names = {p.id: p.name for p in SparePart.query.all()}
    from webapp.models.user import User
    users = {u.id: u.username for u in User.query.all()}
    data_rows = [{
        "business_date": m.business_date, "spare_part_name": spare_names.get(m.spare_part_id, ""),
        "movement_type": m.movement_type, "quantity": str(m.quantity), "requested_by": m.requested_by or "",
        "approved_by": m.approved_by or "", "entered_by": users.get(m.entered_by_user_id, ""),
        "remarks": m.remarks or "", "status": m.status,
    } for m in rows]
    from webapp.services import branding_service
    content = build_export(
        fmt, title="Spare Parts Movement History", filters=filters,
        generated_by=current_user().username, columns=columns, rows=data_rows,
        **branding_service.export_kwargs(),
    )
    record_audit(current_user(), "export", "spare_part_movement",
                 after={"format": fmt, "filters": filters, "row_count": len(data_rows)})
    db.session.commit()
    return Response(content, mimetype=MIME_TYPES[fmt], headers={
        "Content-Disposition": f"attachment; filename=spare_parts_movement_history.{fmt}",
    })
