"""
Historical correction request workflow — an approval queue in front of a
one-time, record-specific, 24-hour edit GRANT (never a second/competing
correction engine; see webapp/models/correction_request.py's own
docstring for the full lifecycle).

Lifecycle for action=correct (the only action a NEW request may use —
Void has no request path at all, see create_request()):

    PENDING --approve--> APPROVED (grant active, nothing on the
                           underlying record has changed yet)
                             |
                             |--- Operator submits ONE correction via the
                             |    normal /correct route within 24h -----> COMPLETED
                             |    (see consume_grant(), called from
                             |    webapp/routes/{dispatches,returns,
                             |    production}.py's correct() after
                             |    record_correction_service.correct_record()
                             |    succeeds)
                             |
                             '--- 24h elapse unused -----------------> EXPIRED
                                  (see expire_if_needed())

    PENDING --reject---> REJECTED (terminal; underlying record never touched)

Approving never calls record_correction_service.correct_record() itself —
that only ever happens when the Operator actually uses their grant, via
the exact same route/UI a same-window direct edit already uses. This is
the key change from the prior round, where approval applied the
correction immediately.
"""
import json
from datetime import timedelta

from sqlalchemy import update

from webapp.extensions import db
from webapp.models.correction_request import (
    ACTION_CORRECT, ACTION_VOID, RECORD_TYPES, STATUS_APPROVED, STATUS_COMPLETED,
    STATUS_EXPIRED, STATUS_PENDING, STATUS_REJECTED,
    CorrectionRequest,
)
from webapp.models.dispatch import Dispatch
from webapp.models.production_record import ProductionRecord
from webapp.models.return_record import ReturnRecord
from webapp.services import dispatch_service, product_usage_service, production_service, returns_service
from webapp.services.audit_service import record_audit
from webapp.services.business_calendar import utcnow

# The one-time edit grant's lifetime, measured from approval (reviewed_at)
# — deliberately the SAME 24-hour figure as record_correction_service.
# OPERATOR_EDIT_WINDOW, but a distinct constant: the two windows start
# from different instants (record creation vs. approval) and are
# conceptually different things that happen to share a duration by
# business-rule coincidence, not by code sharing.
GRANT_WINDOW = timedelta(hours=24)

# A request needs a real, explanatory reason (section 6's own example is
# a full sentence) — this is a floor, not an attempt to judge "meaning".
MIN_REASON_LENGTH = 10

_MODEL = {"dispatch": Dispatch, "returns": ReturnRecord, "production": ProductionRecord}
# Retained ONLY for a legacy action=void request created before this round
# — create_request() refuses to create a new one (see below), so this
# path is unreachable from any current UI, but a pre-existing pending
# void-request must still resolve safely rather than being stuck forever.
_VOID_FN = {
    "dispatch": dispatch_service.void_dispatch,
    "returns": returns_service.void_return,
    "production": production_service.void_production,
}
_VOID_ERROR = {
    "dispatch": dispatch_service.DispatchError,
    "returns": returns_service.ReturnError,
    "production": production_service.ProductionError,
}


class CorrectionRequestError(ValueError):
    """User-facing validation problem — mapped to HTTP 400 by routes."""


def _get_record(record_type, record_id):
    if record_type not in RECORD_TYPES:
        raise CorrectionRequestError(f"unknown record type: {record_type}")
    return db.session.get(_MODEL[record_type], record_id)


def expire_if_needed(request_row, now=None):
    """
    Lazily flips a stale STATUS_APPROVED grant to STATUS_EXPIRED once its
    24-hour window has passed unused — idempotent and race-safe (the
    UPDATE only matches a row still literally APPROVED at this instant,
    so it can never clobber a concurrent consume_grant()). Called before
    every read/decision that depends on grant live-ness (the Requests
    list, the duplicate-active-request check, and the /correct route's
    own eligibility check) so none of them can ever disagree about
    whether a grant is still active. A no-op for any other status.
    """
    if request_row.status != STATUS_APPROVED or request_row.reviewed_at is None:
        return request_row
    now = now or utcnow()
    if now < request_row.reviewed_at + GRANT_WINDOW:
        return request_row
    result = db.session.execute(
        update(CorrectionRequest)
        .where(CorrectionRequest.id == request_row.id, CorrectionRequest.status == STATUS_APPROVED)
        .values(status=STATUS_EXPIRED)
    )
    if result.rowcount:
        # System-triggered (no actor) — this is a lazy sweep, not a user
        # action, but the grant's expiry is still a state change worth a
        # permanent audit trail entry (section 21).
        record_audit(
            None, "grant_expired", "correction_request", entity_id=request_row.id,
            before={"status": STATUS_APPROVED, "record_type": request_row.record_type, "record_id": request_row.record_id},
            after={"status": STATUS_EXPIRED},
        )
    db.session.flush()
    db.session.expire(request_row)
    return request_row


def get_active_grant(record_type, record_id, user, now=None):
    """
    The Operator's own active (approved, unconsumed, unexpired) edit
    grant for this exact record, or None. Reused by
    record_correction_service.operator_has_active_grant() and by the
    Requests page's "does this row still represent a live grant" display.
    """
    now = now or utcnow()
    rows = CorrectionRequest.query.filter(
        CorrectionRequest.record_type == record_type,
        CorrectionRequest.record_id == record_id,
        CorrectionRequest.requested_by == user.id,
        CorrectionRequest.status == STATUS_APPROVED,
    ).all()
    for row in rows:
        expire_if_needed(row, now=now)
        if row.status == STATUS_APPROVED:
            return row
    return None


def create_request(*, record_type, record_id, action, reason, requester, payload=None):
    """
    An Operator may request correction only for a record they themselves
    created (ownership is not relaxed just because it's routed through a
    review queue — the route's own explicit check also enforces this so
    it's never solely a service-layer courtesy). Void is refused outright
    — see module docstring — since it's always directly available to the
    Operator regardless of record age.
    """
    if action == ACTION_VOID:
        raise CorrectionRequestError(
            "Void is available directly for your own record at any time — no request is needed."
        )
    if action != ACTION_CORRECT:
        raise CorrectionRequestError(f"unknown action: {action}")

    record = _get_record(record_type, record_id)
    if record is None:
        raise CorrectionRequestError("record not found")
    reason = (reason or "").strip()
    if not reason:
        raise CorrectionRequestError("A reason is required")
    if len(reason) < MIN_REASON_LENGTH:
        raise CorrectionRequestError(
            f"Please describe the correction in more detail (at least {MIN_REASON_LENGTH} characters)."
        )
    if not payload:
        raise CorrectionRequestError("A correction request needs the requested changes (lines, at minimum)")

    # Section 7: never more than one simultaneously-active request (still
    # pending review, OR an approved grant not yet used/expired) for the
    # same record + Operator — sweep first so a merely stale-in-the-
    # database APPROVED row never wrongly blocks a legitimate new request.
    active = CorrectionRequest.query.filter(
        CorrectionRequest.record_type == record_type,
        CorrectionRequest.record_id == record_id,
        CorrectionRequest.requested_by == requester.id,
        CorrectionRequest.status.in_([STATUS_PENDING, STATUS_APPROVED]),
    ).all()
    for row in active:
        expire_if_needed(row)
        if row.status == STATUS_PENDING:
            raise CorrectionRequestError("A correction request for this record is already pending review.")
        if row.status == STATUS_APPROVED:
            raise CorrectionRequestError("You already have an approved, unused edit grant for this record.")

    request_row = CorrectionRequest(
        record_type=record_type, record_id=record_id, action=action,
        requested_by=requester.id, reason=reason,
        payload_json=json.dumps(payload) if payload else None,
        before_snapshot_json=json.dumps(record.to_dict()),
        status=STATUS_PENDING,
    )
    db.session.add(request_row)
    db.session.flush()
    return request_row


def approve_request(request_row, reviewer, review_note=None):
    """
    Starts the one-time, record-specific, 24-hour edit GRANT — deliberately
    does NOT touch the underlying Dispatch/Return/Production record at
    all (that only happens when the Operator actually uses the grant via
    the normal /correct route; see consume_grant()). The one exception is
    a legacy action=void request (pre-existing this round, unreachable
    from any current UI — see module docstring), which is still applied
    immediately exactly as it always was, then moved straight to
    STATUS_COMPLETED since a void has no follow-up "use the grant" step.
    """
    if request_row.status != STATUS_PENDING:
        raise CorrectionRequestError("This request has already been reviewed")

    if request_row.action == ACTION_VOID:
        record = _get_record(request_row.record_type, request_row.record_id)
        if record is None:
            raise CorrectionRequestError("record no longer exists")
        error_cls = _VOID_ERROR[request_row.record_type]
        try:
            _VOID_FN[request_row.record_type](record, reviewer, request_row.reason)
        except error_cls as e:
            raise CorrectionRequestError(str(e)) from e
        product_usage_service.remove_usage(request_row.record_type, record.id)
        now = utcnow()
        request_row.status = STATUS_COMPLETED
        request_row.reviewed_by = reviewer.id
        request_row.reviewed_at = now
        request_row.review_note = review_note
        request_row.completed_at = now
        db.session.flush()
        return request_row

    request_row.status = STATUS_APPROVED
    request_row.reviewed_by = reviewer.id
    request_row.reviewed_at = utcnow()
    request_row.review_note = review_note
    db.session.flush()
    return request_row


def reject_request(request_row, reviewer, review_note):
    if request_row.status != STATUS_PENDING:
        raise CorrectionRequestError("This request has already been reviewed")
    if not review_note:
        raise CorrectionRequestError("A review note is required to reject a request")
    request_row.status = STATUS_REJECTED
    request_row.reviewed_by = reviewer.id
    request_row.reviewed_at = utcnow()
    request_row.review_note = review_note
    db.session.flush()
    return request_row


def consume_grant(request_row, now=None):
    """
    Atomically marks this request's grant STATUS_COMPLETED — called by
    webapp/routes/{dispatches,returns,production}.py's correct() route
    immediately after record_correction_service.correct_record()
    succeeds for an Operator who was editing via an approved grant
    (never for a same-window direct edit, which has no request row at
    all to consume). Race-safe: the UPDATE only matches a row still
    literally STATUS_APPROVED at this exact instant, so a double-click,
    retry, or genuinely concurrent request can consume the grant at most
    once — every loser's rowcount is 0 and it raises instead of silently
    "succeeding" a second time. Callers must have already applied the
    correction itself before calling this — this function only ever
    updates the request row, never the underlying record.
    """
    now = now or utcnow()
    result = db.session.execute(
        update(CorrectionRequest)
        .where(CorrectionRequest.id == request_row.id, CorrectionRequest.status == STATUS_APPROVED)
        .values(status=STATUS_COMPLETED, completed_at=now)
    )
    if result.rowcount == 0:
        db.session.expire(request_row)
        raise CorrectionRequestError(
            "This edit grant is no longer active (already used, expired, or was never approved)."
        )
    db.session.flush()
    db.session.expire(request_row)
    return request_row


def pending_count():
    """Authoritative backend count of STATUS_PENDING requests — the
    Requests nav badge and the PWA app-icon badge both read this exact
    number, never guessing from however many rows happen to be rendered
    client-side."""
    return CorrectionRequest.query.filter_by(status=STATUS_PENDING).count()
