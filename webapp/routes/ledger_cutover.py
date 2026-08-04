from flask import Blueprint, jsonify, request

from webapp.auth import current_user, roles_required, feature_required
from webapp.extensions import db
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import ledger_cutover_service as svc

ledger_cutover_bp = Blueprint("ledger_cutover", __name__, url_prefix="/api/ledger-cutover")


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


@ledger_cutover_bp.route("", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def list_cutovers():
    return jsonify([c.to_dict() for c in svc.list_cutovers()])


@ledger_cutover_bp.route("/active", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def active_cutover():
    cutover = svc.get_active_cutover()
    return jsonify(cutover.to_dict() if cutover else None)


@ledger_cutover_bp.route("", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def create_draft():
    d = request.get_json(force=True) or {}
    try:
        cutover = svc.create_draft(d.get("effective_date"), d.get("effective_shift"), d.get("reason"), current_user())
    except svc.LedgerCutoverError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify(cutover.to_dict()), 201


@ledger_cutover_bp.route("/<int:cutover_id>", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def get_status(cutover_id):
    try:
        report = svc.cutover_status_report(cutover_id)
    except svc.LedgerCutoverError as e:
        return _error(e, status=404)
    return jsonify(report)


@ledger_cutover_bp.route("/<int:cutover_id>/balances", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def set_balance(cutover_id):
    d = request.get_json(force=True) or {}
    try:
        balance = svc.set_balance(
            cutover_id, d.get("product_id"),
            d.get("cartons", 0), d.get("packs", 0), d.get("pieces", 0),
            current_user(),
        )
    except svc.LedgerCutoverError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify(balance.to_dict())


@ledger_cutover_bp.route("/<int:cutover_id>/import-csv", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def import_csv(cutover_id):
    """Optional convenience import — stages rows onto a draft cutover the
    exact same way manual entry would; never activates anything. The
    uploaded file is read as UTF-8 text from the request body (or a
    multipart 'file' field)."""
    content = None
    if "file" in request.files:
        content = request.files["file"].read().decode("utf-8")
    else:
        content = request.get_data(as_text=True)
    if not content:
        return _error("no CSV content provided")
    try:
        result = svc.import_cutover_csv(cutover_id, content, current_user())
    except svc.LedgerCutoverError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify(result)


@ledger_cutover_bp.route("/<int:cutover_id>/verify", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def verify(cutover_id):
    try:
        cutover = svc.verify_cutover(cutover_id, current_user())
    except svc.LedgerCutoverError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify(cutover.to_dict())


@ledger_cutover_bp.route("/<int:cutover_id>/cancel", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def cancel(cutover_id):
    d = request.get_json(force=True) or {}
    try:
        cutover = svc.cancel_cutover(cutover_id, d.get("reason"), current_user())
    except svc.LedgerCutoverError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify(cutover.to_dict())


@ledger_cutover_bp.route("/<int:cutover_id>/preview-activation", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def preview_activation(cutover_id):
    """Read-only — never writes. Returns the preview token
    /activate must be given back verbatim."""
    try:
        report = svc.preview_activation(cutover_id)
    except svc.LedgerCutoverError as e:
        return _error(e)
    return jsonify(report)


@ledger_cutover_bp.route("/<int:cutover_id>/activate", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def activate(cutover_id):
    d = request.get_json(force=True) or {}
    try:
        result = svc.activate_cutover(
            cutover_id, current_user(),
            preview_token=d.get("preview_token"), confirmation_text=d.get("confirmation_text"),
            backup_confirmed=bool(d.get("backup_confirmed")), reason=d.get("reason"),
        )
    except (svc.LedgerCutoverError, svc.LedgerCutoverConflict) as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify(result)
