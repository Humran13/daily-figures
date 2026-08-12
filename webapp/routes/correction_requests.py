from flask import Blueprint, jsonify, request

from webapp.auth import current_user, login_required, roles_required
from webapp.extensions import db
from webapp.models.correction_request import ACTION_CORRECT, ACTION_VOID, CorrectionRequest
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN
from webapp.services import correction_request_service as svc
from webapp.services import record_correction_service
from webapp.services.audit_service import record_audit
from webapp.services.business_calendar import is_same_business_day

correction_requests_bp = Blueprint("correction_requests", __name__, url_prefix="/api/correction-requests")


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


@correction_requests_bp.route("", methods=["GET"])
@login_required
def list_requests():
    """
    Manager/Super Admin see every request (optionally filtered); an
    Operator sees only their own — read-only status tracking, never a
    review surface for them. Viewer has no access at all (matches
    "Viewer: no editing, no correction requests, read-only").
    """
    user = current_user()
    if user.role not in (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR):
        return jsonify({"error": "forbidden"}), 403

    query = CorrectionRequest.query
    if user.role == ROLE_OPERATOR:
        query = query.filter(CorrectionRequest.requested_by == user.id)
    if request.args.get("status"):
        query = query.filter(CorrectionRequest.status == request.args["status"])
    if request.args.get("record_type"):
        query = query.filter(CorrectionRequest.record_type == request.args["record_type"])
    rows = query.order_by(CorrectionRequest.created_at.desc()).limit(200).all()
    return jsonify([r.to_dict() for r in rows])


@correction_requests_bp.route("", methods=["POST"])
@roles_required(ROLE_OPERATOR)
def create_request():
    """
    Operator-only — Manager/Super Admin never need this (they correct/void
    directly, any day). Only for a record the Operator themselves created,
    and only once it is no longer same-day (a same-day record should use
    the direct Edit/Void action instead — this route refuses to create a
    request for one, so the two paths never overlap/duplicate).
    """
    user = current_user()
    d = request.get_json(force=True) or {}
    record_type = d.get("record_type")
    record_id = d.get("record_id")
    action = d.get("action")
    reason = (d.get("reason") or "").strip()
    if record_type not in ("dispatch", "returns", "production"):
        return _error("record_type must be one of dispatch, returns, production")
    if action not in (ACTION_CORRECT, ACTION_VOID):
        return _error("action must be 'correct' or 'void'")

    record = record_correction_service.get_record(record_type, record_id)
    if record is None:
        return jsonify({"error": "not found"}), 404
    if record.created_by != user.id:
        return jsonify({"error": "forbidden — you may only request correction/void for your own record"}), 403
    if is_same_business_day(record.date):
        return _error("this record is still same-day — use the direct Edit/Void action instead of a request", 409)

    try:
        req = svc.create_request(
            record_type=record_type, record_id=record_id, action=action,
            reason=reason, requester=user, payload=d.get("payload"),
        )
    except svc.CorrectionRequestError as e:
        db.session.rollback()
        return _error(e)

    record_audit(user, "create", "correction_request", entity_id=req.id, after=req.to_dict())
    db.session.commit()
    return jsonify(req.to_dict()), 201


@correction_requests_bp.route("/<int:request_id>/approve", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
def approve_request(request_id):
    user = current_user()
    req = db.session.get(CorrectionRequest, request_id)
    if req is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    before = req.to_dict()
    try:
        svc.approve_request(req, user, review_note=d.get("review_note"))
    except svc.CorrectionRequestError as e:
        db.session.rollback()
        return _error(e)

    record_audit(user, "approve", "correction_request", entity_id=req.id, before=before, after=req.to_dict())
    db.session.commit()
    return jsonify(req.to_dict())


@correction_requests_bp.route("/<int:request_id>/reject", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
def reject_request(request_id):
    user = current_user()
    req = db.session.get(CorrectionRequest, request_id)
    if req is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    before = req.to_dict()
    try:
        svc.reject_request(req, user, review_note=d.get("review_note"))
    except svc.CorrectionRequestError as e:
        db.session.rollback()
        return _error(e)

    record_audit(user, "reject", "correction_request", entity_id=req.id, before=before, after=req.to_dict())
    db.session.commit()
    return jsonify(req.to_dict())
