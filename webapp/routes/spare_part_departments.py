"""
Operational (factory) departments that receive spare parts — unrelated to
webapp.models.section's "Store Department" application-section concept
(see webapp/models/operational_department.py's docstring). Reads stay
login_required, not elevated-only, because Operator needs this list to
populate the Stock Out "Department" dropdown.
"""
import logging

from flask import Blueprint, jsonify, request
from sqlalchemy.exc import SQLAlchemyError

from webapp.auth import current_user, login_required, roles_required
from webapp.extensions import db
from webapp.models.operational_department import SparePartDepartment
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import spare_part_service as svc
from webapp.services.audit_service import record_audit

spare_part_departments_bp = Blueprint("spare_part_departments", __name__, url_prefix="/api/spare-parts/departments")

logger = logging.getLogger(__name__)


@spare_part_departments_bp.route("", methods=["GET"])
@login_required
def list_departments():
    include_inactive = request.args.get("include_inactive") == "1"
    query = SparePartDepartment.query
    if not include_inactive:
        query = query.filter_by(active=True)
    return jsonify([d.to_dict() for d in query.order_by(SparePartDepartment.name).all()])


@spare_part_departments_bp.route("", methods=["POST"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def create_department():
    d = request.get_json(force=True) or {}
    try:
        department = svc.create_department(d.get("name"))
    except svc.SparePartError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "create", "spare_part_department", entity_id=department.id, after=department.to_dict())
    db.session.commit()
    return jsonify(department.to_dict()), 201


@spare_part_departments_bp.route("/<int:department_id>", methods=["PATCH"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def update_department(department_id):
    department = db.session.get(SparePartDepartment, department_id)
    if department is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    before = department.to_dict()
    try:
        svc.update_department(department, d)
    except svc.SparePartError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    record_audit(current_user(), "update", "spare_part_department", entity_id=department.id, before=before, after=department.to_dict())
    db.session.commit()
    return jsonify(department.to_dict())


@spare_part_departments_bp.route("/<int:department_id>", methods=["DELETE"])
@roles_required(ROLE_SUPER_ADMIN)
def delete_department(department_id):
    department = db.session.get(SparePartDepartment, department_id)
    if department is None:
        return jsonify({"error": "not found"}), 404
    try:
        removed = svc.permanently_delete_department(department, current_user())
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("permanent delete of department %s failed", department_id)
        return jsonify({"error": "Delete failed. Nothing was removed; please try again."}), 500
    return jsonify({"ok": True, "removed": removed})
