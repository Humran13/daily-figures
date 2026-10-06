import logging

from flask import Blueprint, jsonify, request
from sqlalchemy.exc import SQLAlchemyError

from webapp.auth import current_user, login_required, roles_required
from webapp.extensions import db
from webapp.models.machine import Machine, MachineAlias
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import spare_part_service as svc
from webapp.services.audit_service import record_audit

spare_part_machines_bp = Blueprint("spare_part_machines", __name__, url_prefix="/api/spare-parts/machines")

logger = logging.getLogger(__name__)


def _to_dict(machine):
    d = machine.to_dict()
    d["aliases"] = [a.raw_text for a in MachineAlias.query.filter_by(machine_id=machine.id).all()]
    return d


@spare_part_machines_bp.route("", methods=["GET"])
@login_required
def list_machines():
    include_inactive = request.args.get("include_inactive") == "1"
    department_id = request.args.get("department_id")
    query = Machine.query
    if not include_inactive:
        query = query.filter_by(active=True)
    if department_id:
        try:
            query = query.filter_by(department_id=int(department_id))
        except ValueError:
            return jsonify({"error": "invalid department_id"}), 400
    return jsonify([_to_dict(m) for m in query.order_by(Machine.name).all()])


@spare_part_machines_bp.route("", methods=["POST"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def create_machine():
    d = request.get_json(force=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    from webapp.services.spare_part_service import unique_machine_code
    machine = Machine(
        code=unique_machine_code(name), name=name, active=True, notes=d.get("notes"),
        department_id=d.get("department_id"),
    )
    db.session.add(machine)
    db.session.flush()
    db.session.add(MachineAlias(machine_id=machine.id, raw_text=name, normalized_text=name.casefold()))
    record_audit(current_user(), "create", "machine", entity_id=machine.id, after=machine.to_dict())
    db.session.commit()
    return jsonify(_to_dict(machine)), 201


@spare_part_machines_bp.route("/<int:machine_id>", methods=["PATCH"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def update_machine(machine_id):
    machine = db.session.get(Machine, machine_id)
    if machine is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    before = machine.to_dict()
    if "name" in d:
        name = (d.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name cannot be empty"}), 400
        machine.name = name
    if "notes" in d:
        machine.notes = d.get("notes")
    if "department_id" in d:
        machine.department_id = d.get("department_id")
    if "active" in d:
        machine.active = bool(d["active"])
    record_audit(current_user(), "update", "machine", entity_id=machine.id, before=before, after=machine.to_dict())
    db.session.commit()
    return jsonify(_to_dict(machine))


@spare_part_machines_bp.route("/<int:machine_id>", methods=["DELETE"])
@roles_required(ROLE_SUPER_ADMIN)
def delete_machine(machine_id):
    machine = db.session.get(Machine, machine_id)
    if machine is None:
        return jsonify({"error": "not found"}), 404
    try:
        removed = svc.permanently_delete_machine(machine, current_user())
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("permanent delete of machine %s failed", machine_id)
        return jsonify({"error": "Delete failed. Nothing was removed; please try again."}), 500
    return jsonify({"ok": True, "removed": removed})


@spare_part_machines_bp.route("/<int:machine_id>/aliases", methods=["POST"])
@roles_required(ROLE_MANAGER, ROLE_SUPER_ADMIN)
def repoint_alias(machine_id):
    """Re-points an existing alias's normalized_text to this machine, e.g.
    admin cleanup after import flagged a compound/ambiguous machine cell —
    the original raw text is kept, only which Machine it resolves to changes."""
    machine = db.session.get(Machine, machine_id)
    if machine is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    normalized_text = (d.get("normalized_text") or "").strip().casefold()
    if not normalized_text:
        return jsonify({"error": "normalized_text is required"}), 400
    alias = MachineAlias.query.filter_by(normalized_text=normalized_text).first()
    if alias is None:
        return jsonify({"error": "no alias with that normalized_text exists"}), 404
    before = alias.to_dict()
    alias.machine_id = machine.id
    record_audit(current_user(), "repoint_alias", "machine_alias", entity_id=alias.id, before=before, after=alias.to_dict())
    db.session.commit()
    return jsonify(alias.to_dict())
