from flask import Blueprint, jsonify, request

from webapp.auth import current_user, feature_required, roles_required
from webapp.extensions import db
from webapp.models.dispatch import SHIFTS
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import daily_review_service as svc

daily_review_bp = Blueprint("daily_review", __name__, url_prefix="/api/daily-review")


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


def _require_date_shift(d):
    date = d.get("date")
    shift = d.get("shift")
    if not date or shift not in SHIFTS:
        return None, None, _error(f"date and shift (one of {SHIFTS}) are required")
    return date, shift, None


@daily_review_bp.route("", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def summary():
    date, shift, err = _require_date_shift(request.args)
    if err:
        return err
    return jsonify(svc.build_summary(date, shift))


@daily_review_bp.route("/mark-reviewed", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def mark_reviewed():
    d = request.get_json(force=True) or {}
    date, shift, err = _require_date_shift(d)
    if err:
        return err
    if not d.get("product_id"):
        return _error("product_id is required")
    user = current_user()
    try:
        row = svc.mark_reviewed(
            date, shift, int(d["product_id"]), user,
            edited=bool(d.get("edited")), reason=d.get("reason"),
        )
    except svc.ReviewConflict as e:
        db.session.rollback()
        return _error(e, status=409)
    except svc.ReviewError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify({"product_id": row.product_id, "state": row.state})


@daily_review_bp.route("/mark-skipped", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def mark_skipped():
    d = request.get_json(force=True) or {}
    date, shift, err = _require_date_shift(d)
    if err:
        return err
    if not d.get("product_id"):
        return _error("product_id is required")
    user = current_user()
    try:
        row = svc.mark_skipped(date, shift, int(d["product_id"]), user)
    except svc.ReviewConflict as e:
        db.session.rollback()
        return _error(e, status=409)
    except svc.ReviewError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify({"product_id": row.product_id, "state": row.state})


@daily_review_bp.route("/submit", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def submit():
    d = request.get_json(force=True) or {}
    date, shift, err = _require_date_shift(d)
    if err:
        return err
    user = current_user()
    try:
        svc.submit_review(date, shift, user)
    except svc.ReviewError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify(svc.session_view(date, shift))


@daily_review_bp.route("/reopen", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("daily_figures")
def reopen():
    d = request.get_json(force=True) or {}
    date, shift, err = _require_date_shift(d)
    if err:
        return err
    user = current_user()
    try:
        svc.reopen_review(date, shift, user, d.get("reason"))
    except svc.ReviewError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify(svc.session_view(date, shift))
