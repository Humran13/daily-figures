from flask import Blueprint, jsonify, request

from webapp.auth import current_user, login_required
from webapp.extensions import db
from webapp.services import push_service

push_bp = Blueprint("push", __name__, url_prefix="/api/push")


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


@push_bp.route("/vapid-public-key", methods=["GET"])
@login_required
def vapid_public_key():
    """
    None (not an error) when push isn't configured for this deployment —
    the frontend feature-detects on this being falsy and simply never
    offers the "Enable notifications" opt-in in that case.
    """
    return jsonify({"key": push_service.vapid_public_key(), "configured": push_service.is_configured()})


@push_bp.route("/subscribe", methods=["POST"])
@login_required
def subscribe():
    """
    User-initiated opt-in only — the browser's own Notification.
    requestPermission() prompt (never auto-requested) must already have
    been granted before the frontend ever calls this. Stores the
    subscription against the AUTHENTICATED session's user, never a
    client-supplied user id.
    """
    user = current_user()
    d = request.get_json(force=True) or {}
    try:
        row = push_service.save_subscription(user, d.get("subscription"))
    except push_service.PushSubscriptionError as e:
        db.session.rollback()
        return _error(e)
    db.session.commit()
    return jsonify(row.to_dict()), 201


@push_bp.route("/unsubscribe", methods=["POST"])
@login_required
def unsubscribe():
    d = request.get_json(force=True) or {}
    push_service.remove_subscription(d.get("endpoint"))
    db.session.commit()
    return jsonify({"ok": True})
