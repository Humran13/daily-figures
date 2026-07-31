from flask import Blueprint, jsonify, request

from webapp.auth import current_user, login_required, roles_required, feature_required
from webapp.extensions import db
from webapp.models.dispatch import SHIFTS
from webapp.models.product import Product
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN
from webapp.services import daily_entry_status_service as svc

daily_entry_status_bp = Blueprint("daily_entry_status", __name__, url_prefix="/api/daily-entry-status")


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


def _parse_request(d):
    date = d.get("date") or request.args.get("date")
    shift = d.get("shift") or request.args.get("shift")
    product_id = d.get("product_id") or request.args.get("product_id")
    if not date or shift not in SHIFTS or not product_id:
        return None, None, None, _error(f"date, shift (one of {SHIFTS}), and product_id are required")
    product = db.session.get(Product, int(product_id))
    if product is None:
        return None, None, None, _error("product does not exist", status=404)
    return date, shift, product, None


@daily_entry_status_bp.route("", methods=["GET"])
@login_required
@feature_required("daily_figures")
def get_status():
    date, shift, product, err = _parse_request({})
    if err:
        return err
    return jsonify(svc.status_view(date, shift, product.id))


@daily_entry_status_bp.route("/lock", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("daily_figures")
def acquire_lock():
    d = request.get_json(force=True) or {}
    date, shift, product, err = _parse_request(d)
    if err:
        return err
    user = current_user()
    try:
        svc.acquire_lock(date, shift, product.id, user)
    except svc.DailyEntryStatusConflict as e:
        db.session.rollback()
        return _error(e, status=409)
    db.session.commit()
    return jsonify(svc.status_view(date, shift, product.id))


@daily_entry_status_bp.route("/lock", methods=["DELETE"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("daily_figures")
def release_lock():
    d = request.get_json(force=True) or {}
    date, shift, product, err = _parse_request(d)
    if err:
        return err
    user = current_user()
    svc.release_lock(date, shift, product.id, user)
    db.session.commit()
    return jsonify(svc.status_view(date, shift, product.id))


@daily_entry_status_bp.route("/no-activity", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("daily_figures")
def no_activity():
    d = request.get_json(force=True) or {}
    date, shift, product, err = _parse_request(d)
    if err:
        return err
    user = current_user()
    # Manager/Super Admin correction authority (Stage 7 section 3) bypasses
    # ownership locking entirely, same as the Daily Figures upsert route —
    # only an Operator can be blocked by someone else's in-progress lock.
    if user.role == ROLE_OPERATOR:
        conflict = svc.check_operator_conflict(date, shift, product.id, user)
        if conflict:
            return _error(conflict, status=409)
    try:
        svc.mark_no_activity(date, shift, product.id, user)
    except svc.DailyEntryStatusConflict as e:
        db.session.rollback()
        return _error(e, status=409)
    db.session.commit()
    return jsonify(svc.status_view(date, shift, product.id))


@daily_entry_status_bp.route("/takeover", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def takeover():
    d = request.get_json(force=True) or {}
    date, shift, product, err = _parse_request(d)
    if err:
        return err
    actor = current_user()
    try:
        svc.takeover_lock(date, shift, product.id, actor)
    except svc.DailyEntryStatusError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify(svc.status_view(date, shift, product.id))


@daily_entry_status_bp.route("/reopen", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def reopen():
    d = request.get_json(force=True) or {}
    date, shift, product, err = _parse_request(d)
    if err:
        return err
    actor = current_user()
    try:
        svc.reopen(date, shift, product.id, actor, d.get("reason"))
    except svc.DailyEntryStatusError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify(svc.status_view(date, shift, product.id))
