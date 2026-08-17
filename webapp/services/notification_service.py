"""
WHO gets notified for correction-request lifecycle events, and what the
message says — the actual transport is push_service.py (a graceful no-op
without VAPID configuration; see its own docstring). The guaranteed,
always-on equivalent is entirely independent of whether push is
configured or delivered at all:
  - Manager/Super Admin: the Requests top-level nav badge (authoritative
    backend pending count — see correction_request_service.pending_count())
    and the Requests page itself.
  - Operator: their own record's action-button matrix reflects an active
    grant immediately (Edit replaces Request Correction — see
    record_correction_service.operator_has_active_grant()), and their own
    request list (GET /api/correction-requests, self-scoped) shows the
    current status — this IS the in-app "state indication" section 13
    asks for; a separate notifications inbox was deliberately not built
    for it (reuse existing surfaces, never a second competing system).

Every function here is called AFTER the triggering database transaction
has already committed, and never raises — a notification failure must
never surface as an error on the request that triggered it.
"""
import logging

from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN, User
from webapp.services import push_service

logger = logging.getLogger(__name__)

REQUESTS_URL = "/requests.html"

_RECORD_LABELS = {"dispatch": "Dispatch", "returns": "Return", "production": "Production"}


def _record_label(record_type, record_id):
    return f"{_RECORD_LABELS.get(record_type, record_type.title())} #{record_id}"


def notify_new_correction_request(request_row):
    """
    One push, to every Manager/Super Admin with an opted-in subscription,
    per request creation — never duplicated, since this is called exactly
    once from POST /api/correction-requests immediately after the single
    INSERT that creates the row.
    """
    try:
        from webapp.services import correction_request_service

        manager_ids = [
            u.id for u in User.query.filter(
                User.role.in_([ROLE_MANAGER, ROLE_SUPER_ADMIN]), User.active.is_(True),
            ).all()
        ]
        requester_name = request_row.requester.username if request_row.requester else "an operator"
        label = _record_label(request_row.record_type, request_row.record_id)
        push_service.notify_users(
            manager_ids, "Daily Figures",
            f"New correction request from {requester_name} for {label}.",
            REQUESTS_URL,
            # The authoritative count at send time — carried in the push
            # payload so the service worker's own push handler can update
            # the app-icon badge (where the platform supports background
            # Badging) even while the PWA is fully closed, not just once
            # someone opens it (see static/sw.js's `push` handler).
            badge_count=correction_request_service.pending_count(),
        )
    except Exception:
        logger.exception("notify_new_correction_request failed (non-fatal — in-app badge is unaffected)")


def notify_request_decided(request_row, approved):
    """
    One push to the requesting Operator when their request is approved
    (a grant just started) or rejected. Never includes business figures
    or the review note's full content beyond a short rejection reason —
    nothing sensitive on a lock screen.
    """
    try:
        label = _record_label(request_row.record_type, request_row.record_id)
        if approved:
            title = "Correction approved"
            body = f"You may now edit {label}. This permission expires in 24 hours."
        else:
            title = "Correction request rejected"
            body = f"Your request for {label} was rejected."
        push_service.notify_users([request_row.requested_by], title, body, REQUESTS_URL)
    except Exception:
        logger.exception("notify_request_decided failed (non-fatal — the Requests page status is unaffected)")
