from flask import Blueprint, jsonify, request

from webapp.auth import current_user, login_required, roles_required
from webapp.extensions import db
from webapp.models.correction_request import ACTION_CORRECT, CorrectionRequest
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN
from webapp.services import correction_request_service as svc
from webapp.services import record_correction_service
from webapp.services.audit_service import record_audit

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
    "Viewer: no editing, no correction requests, read-only"). Sweeps any
    stale-in-the-database APPROVED grant past its 24h window to EXPIRED
    before serializing, so this list is never stale about a grant's
    live-ness (see correction_request_service.expire_if_needed()).
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
    if request.args.get("record_id"):
        query = query.filter(CorrectionRequest.record_id == int(request.args["record_id"]))
    rows = query.order_by(CorrectionRequest.created_at.desc()).limit(200).all()
    for row in rows:
        svc.expire_if_needed(row)
    db.session.commit()
    return jsonify([r.to_dict() for r in rows])


@correction_requests_bp.route("/pending-count", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
def pending_count():
    """
    Authoritative backend count for the top-level Requests nav badge and
    the PWA app-icon badge — Manager/Super Admin only (matches who sees
    the badge at all; Operator/Viewer never call this).
    """
    return jsonify({"count": svc.pending_count()})


@correction_requests_bp.route("/grant-status", methods=["GET"])
@roles_required(ROLE_OPERATOR)
def grant_status():
    """
    Does the CURRENT Operator hold an active edit grant for this exact
    record right now — read by dispatch.html/returns.html/production.html's
    openDetail() so the button matrix (Edit vs. Request Correction) can
    reflect it. UX convenience only; the /correct route re-derives this
    itself server-side rather than trusting anything from here.
    """
    record_type = request.args.get("record_type")
    record_id = request.args.get("record_id")
    if record_type not in ("dispatch", "returns", "production") or not record_id:
        return _error("record_type and record_id are required")
    user = current_user()
    grant = svc.get_active_grant(record_type, int(record_id), user)
    db.session.commit()  # persist any lazy-expiry flip performed while checking
    if grant is None:
        return jsonify({"has_active_grant": False})
    return jsonify({"has_active_grant": True, "request_id": grant.id, "grant_expires_at_label": grant.to_dict()["grant_expires_at_label"]})


@correction_requests_bp.route("", methods=["POST"])
@roles_required(ROLE_OPERATOR)
def create_request():
    """
    Operator-only — Manager/Super Admin never need this (they correct
    directly, any day; they also void directly, any day). Only for a
    record the Operator themselves created, and only once direct Edit is
    no longer available for it — either the 24-hour window
    (record.created_at + 24h) has closed, or the record is currently
    void (operator_can_directly_edit() returns False in both cases; see
    its own docstring). A still-editable record should use the direct
    Edit action instead; this route refuses to create a request for one,
    so the two paths never overlap/duplicate. A voided record IS eligible
    for a correction request (section 1) — approval only ever grants a
    one-time edit that must leave it void; see record_correction_
    service.correct_record()'s own void-preserving handling. Void itself
    (the ACTION, not a request against a void record) still has no
    request path at all — see correction_request_service.create_request()'s
    own refusal of action=void.
    """
    user = current_user()
    d = request.get_json(force=True) or {}
    record_type = d.get("record_type")
    record_id = d.get("record_id")
    action = d.get("action")
    reason = d.get("reason")
    if record_type not in ("dispatch", "returns", "production"):
        return _error("record_type must be one of dispatch, returns, production")
    if action != ACTION_CORRECT:
        return _error("action must be 'correct' — Void is available directly at any time, no request needed")

    record = record_correction_service.get_record(record_type, record_id)
    if record is None:
        return jsonify({"error": "not found"}), 404
    if record.created_by != user.id:
        return jsonify({"error": "forbidden — you may only request a correction for your own record"}), 403
    if record_correction_service.operator_can_directly_edit(record, user):
        return _error("this record is still within its 24-hour edit window — use the direct Edit action instead", 409)

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
    from webapp.services import notification_service
    notification_service.notify_new_correction_request(req)
    return jsonify(req.to_dict()), 201


@correction_requests_bp.route("/<int:request_id>/approve", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
def approve_request(request_id):
    """
    Starts the requesting Operator's one-time, record-specific, 24-hour
    edit grant — never immediately touches the underlying record (see
    correction_request_service.approve_request()'s own docstring).
    """
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
    from webapp.services import notification_service
    notification_service.notify_request_decided(req, approved=True)
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
    from webapp.services import notification_service
    notification_service.notify_request_decided(req, approved=False)
    return jsonify(req.to_dict())
