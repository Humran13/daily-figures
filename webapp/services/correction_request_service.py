"""
Historical correction/void request workflow — the controlled path an
Operator uses once a record's own business Date is no longer today
(Africa/Kampala; see business_calendar.py and record_correction_service.
operator_can_directly_edit()). Creating a request never mutates the
underlying Dispatch/Return/Production record; APPROVING one simply
invokes the exact same existing mutation functions Manager/Super Admin
already use directly for a same-day/any-day correction or void
(record_correction_service.correct_record() / dispatch_service.
void_dispatch() / returns_service.void_return() / production_service.
void_production()) — this module is purely an approval queue in front of
that existing, already-audited machinery, never a second correction
engine. Rejecting one changes nothing on the underlying record at all.
"""
import json
from datetime import datetime, timezone

from webapp.extensions import db
from webapp.models.correction_request import (
    ACTION_CORRECT, ACTION_VOID, RECORD_TYPES, STATUS_APPROVED, STATUS_PENDING, STATUS_REJECTED,
    CorrectionRequest,
)
from webapp.models.dispatch import Dispatch
from webapp.models.production_record import ProductionRecord
from webapp.models.return_record import ReturnRecord
from webapp.services import dispatch_service, product_usage_service, production_service, record_correction_service, returns_service


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

_MODEL = {"dispatch": Dispatch, "returns": ReturnRecord, "production": ProductionRecord}
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


def create_request(*, record_type, record_id, action, reason, requester, payload=None):
    """
    An Operator may request correction/void only for a record they
    themselves created (ownership is not relaxed just because it's now
    routed through a review queue instead of a direct mutation — see
    the route's own explicit check, which also enforces this so the
    ownership rule is never solely a service-layer courtesy).
    """
    if action not in (ACTION_CORRECT, ACTION_VOID):
        raise CorrectionRequestError(f"unknown action: {action}")
    record = _get_record(record_type, record_id)
    if record is None:
        raise CorrectionRequestError("record not found")
    if not reason:
        raise CorrectionRequestError("A reason is required")
    if action == ACTION_CORRECT and not payload:
        raise CorrectionRequestError("A correction request needs the requested changes (lines, at minimum)")

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
    Applies the SAME correct_record()/void_*() call a Manager/Super Admin
    would make directly, with `reviewer` as the actor of record (not the
    original requester) — the audit trail for the underlying record shows
    who actually authorized the change, exactly like every other
    Manager/Super Admin correction in this app.
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
    else:
        payload = json.loads(request_row.payload_json) if request_row.payload_json else {}
        error_cls = _VOID_ERROR[request_row.record_type]
        try:
            record_correction_service.correct_record(
                request_row.record_type, request_row.record_id,
                reason=request_row.reason, actor=reviewer, **payload,
            )
        except record_correction_service.RecordCorrectionConflict as e:
            raise CorrectionRequestError(str(e)) from e
        except (record_correction_service.RecordCorrectionError, error_cls) as e:
            raise CorrectionRequestError(str(e)) from e
        except TypeError as e:
            # A stored payload missing/mismatching correct_record()'s
            # expected fields (e.g. no "lines") — a clean 400 rather than
            # an unhandled 500, so a malformed request can be rejected by
            # a reviewer instead of crashing the request.
            raise CorrectionRequestError(f"malformed correction payload: {e}") from e

    request_row.status = STATUS_APPROVED
    request_row.reviewed_by = reviewer.id
    request_row.reviewed_at = _utcnow()
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
    request_row.reviewed_at = _utcnow()
    request_row.review_note = review_note
    db.session.flush()
    return request_row
