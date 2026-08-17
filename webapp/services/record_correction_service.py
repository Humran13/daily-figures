"""
Manager/Super-Administrator "Correct Record" workflow (final pre-
deployment correction) for Dispatch/Returns/Production history — amends
an existing record IN PLACE (same record id, full audit trail) instead of
the manual reopen -> navigate to the module -> edit -> finalize round
trip, and instead of Duplicate (which creates a SEPARATE new record and
leaves the original mistake standing). Reopen/Void/Duplicate/Print all
remain exactly as they were — this is an additional action alongside
them, never a replacement.

If the record is currently finalized, correcting it internally reopens
it, applies the line/notes changes, re-validates, and refinalizes it, all
within one transaction — the caller never has to do those steps
separately. If any part fails, nothing is kept: the original finalized
record is left completely unchanged (see webapp/routes/*.py's correct()
handlers, which roll back and return a clear error).

Concurrency: an optional `expected_updated_at` (the record's own
`updated_at`, ISO-formatted, as last seen by the caller) is checked
before anything is touched. A mismatch means someone else's correction
already landed — this raises RecordCorrectionConflict rather than
silently overwriting their change (webapp's only real "version" signal
for these records is `updated_at`, which every mutation already bumps
via its column's `onupdate=`, so no new schema field is needed).
"""
from datetime import timedelta

from webapp.extensions import db
from webapp.models.dispatch import Dispatch, DispatchLine
from webapp.models.production_record import ProductionLine, ProductionRecord
from webapp.models.return_record import ReturnLine, ReturnRecord
from webapp.services import dispatch_service, production_service, product_usage_service, returns_service
from webapp.services.audit_service import record_audit
from webapp.services.business_calendar import utcnow

STATUS_VOID = "void"
STATUS_FINALIZED = "finalized"
STATUS_DRAFT = "draft"

# Final Operator correction/void/requests package — the Operator direct-
# Edit window is now an EXACT 24-hour window measured from the record's
# own created_at (never the business Date, never the browser clock; see
# operator_can_directly_edit()'s own docstring for why). This deliberately
# supersedes the earlier "same business day" rule from a prior round.
OPERATOR_EDIT_WINDOW = timedelta(hours=24)


def operator_can_directly_edit(record, user, now=None):
    """
    Operator direct Edit window: an Operator may directly Edit a record
    only when they created it AND less than exactly 24 hours have elapsed
    since its own `created_at` (server-authoritative, UTC-stored, Kampala
    only for DISPLAY — see business_calendar.format_kampala_datetime()).
    This is deliberately NOT "same business date" — a record entered at
    11 PM is editable until 11 PM the next calendar day, and a record
    entered at 1 AM stops being editable at 1 AM the same calendar day.
    Once this window closes, direct correction is refused; the Operator
    must submit a Request Correction instead (see
    correction_request_service.py) — UNLESS they hold an active,
    unconsumed approval grant for this exact record (see
    operator_has_active_grant() below), in which case Edit is allowed
    again for one single correction. Manager/Super Admin are never
    subject to this check — they call this only for Operator-role
    callers. `now` is accepted for deterministic testing (never trust a
    real wall-clock read in a test); production code omits it and gets
    the real current instant.
    """
    if record.created_by != user.id:
        return False
    if record.status == STATUS_VOID:
        # Full targeted Operator correction/void/requests/notification
        # package, section 1: a voided record is NEVER directly editable,
        # regardless of age — it always goes through Request Correction
        # + Manager/Super Admin approval + a one-time grant instead (see
        # correct_record()'s own void handling below). Without this
        # check, a *freshly* voided record (<24h old) would otherwise
        # read as "still directly editable" here while correct_record()
        # itself refuses a direct (non-grant) edit on any void record —
        # a dead end where neither Edit nor Request Correction could ever
        # be reached. This keeps the two checks in agreement.
        return False
    now = now or utcnow()
    return now < record.created_at + OPERATOR_EDIT_WINDOW


def operator_can_directly_void(record, user):
    """
    Operator direct Void — deliberately UNCONDITIONAL on record age
    (unlike Edit above): an Operator may Void their own record at any
    time after creation, no matter how old it is. There is no "Request
    Void" — Void is always self-serve for the Operator's own record; see
    correction_request_service.create_request(), which now refuses to
    create a void-type request at all. Ownership is the only gate.
    """
    return record.created_by == user.id


def operator_has_active_grant(record_type, record_id, user, now=None):
    """
    True when this exact Operator holds an active (STATUS_APPROVED, not
    yet consumed, not yet expired) one-time correction grant for this
    exact record_type/record_id — see correction_request_service.py's
    grant lifecycle (approve_request() starts it; consume_grant() ends it
    on first successful use; expire_if_needed() ends it after 24h
    unused). Reused by every one of dispatches.py/returns.py/
    production.py's /correct routes so "does this Operator have an
    editable-via-grant record right now" is answered in exactly one
    place. Lazily expires a stale-but-still-DB-APPROVED grant as a side
    effect of checking it, so the Requests page and this check never
    disagree about whether a grant is still live.
    """
    from webapp.services import correction_request_service
    return correction_request_service.get_active_grant(record_type, record_id, user, now=now) is not None

# One small registry so this logic is written once and shared by all three
# source books (which are otherwise near-identical modules with no common
# base class) rather than three copies of the same correction flow.
_REGISTRY = {
    "dispatch": {
        "model": Dispatch, "line_model": DispatchLine, "fk": "dispatch_id",
        "reopen": dispatch_service.reopen_dispatch, "finalize": dispatch_service.finalize_dispatch,
        "build_line": dispatch_service.build_line, "error": dispatch_service.DispatchError,
        "notes_field": "notes",
        # The entity_type every OTHER audit action for this source book
        # already uses (see webapp/routes/dispatches.py's record_audit()
        # calls) — "dispatch" here, but singular "return" for Returns
        # below, an existing inconsistency this service must match rather
        # than invent a fourth naming scheme. This keeps audit_history()
        # able to find a record's full create/finalize/reopen/void/
        # correct trail under the one entity_type it was actually logged
        # under.
        "audit_entity_type": "dispatch",
    },
    "returns": {
        "model": ReturnRecord, "line_model": ReturnLine, "fk": "return_id",
        "reopen": returns_service.reopen_return, "finalize": returns_service.finalize_return,
        "build_line": returns_service.build_line, "error": returns_service.ReturnError,
        "notes_field": "remarks",
        "audit_entity_type": "return",
    },
    "production": {
        "model": ProductionRecord, "line_model": ProductionLine, "fk": "production_id",
        "reopen": production_service.reopen_production, "finalize": production_service.finalize_production,
        "build_line": production_service.build_line, "error": production_service.ProductionError,
        "notes_field": "remarks",
        "audit_entity_type": "production",
    },
}

SOURCE_TYPES = list(_REGISTRY)


class RecordCorrectionError(ValueError):
    """User-facing validation problem — mapped to HTTP 400 by routes."""


class RecordCorrectionConflict(ValueError):
    """The record changed since the caller last loaded it — mapped to HTTP 409 by routes."""


def get_record(source_type, record_id):
    if source_type not in _REGISTRY:
        raise RecordCorrectionError(f"unknown record type: {source_type}")
    return db.session.get(_REGISTRY[source_type]["model"], record_id)


def correct_record(source_type, record_id, *, lines, reason, actor, notes=None, expected_updated_at=None,
                    date=None, customer_id=None, new_customer_name=None, sales_category_id=None,
                    shift=None, returned_by_customer_id=None, returned_by_name=None, signed_by_name=None,
                    via_request_id=None):
    """
    lines: a list of {id, product_id, cartons, packs, pieces, line_notes}
      dicts describing the FULL corrected line set —
        - an entry whose `id` matches an existing line updates it.
        - an entry with no `id` (or one that matches nothing) adds a new line.
        - any EXISTING line not represented here is removed.
    notes: replacement value for the record's own free-text field
      (Dispatch's `notes`, Returns'/Production's `remarks`) — pass None to
      leave it unchanged.
    reason: required — the correction's own justification. Recorded in the
      audit trail, not written onto the record's business notes field
      (that field is for business content, not this action's paper trail
      — mirrors Reset Daily Values' identical separation).
    date: optional replacement for the record's own `date` — every source
      type has this column, so this applies generically. Because every
      Issued/Production/Returns figure everywhere in this app is computed
      live from these rows (see stock_service.py's dispatch_issued_base_qty()
      etc. — never a stored/cached total), moving a record's date here is
      enough on its own for the old date to lose its contribution and the
      new date to gain it exactly once — no manual ledger patching.
    customer_id / new_customer_name / sales_category_id: optional recipient
      correction — DISPATCH ONLY. Delegates to dispatch_service.update_recipient()
      for the exact same resolve/validate/snapshot logic the main create/
      edit paths use — never a second, competing implementation.
    returned_by_customer_id / returned_by_name / signed_by_name: optional
      "returned by" / signer correction — RETURNS ONLY. Delegates to
      returns_service.update_header() (the same header-update function the
      draft-editing endpoints already use) — reachable here because the
      record has already been reopened to draft status above when it was
      finalized, so update_header()'s own draft-only guard is always
      satisfied by this point.
    shift: optional Day/Night correction — PRODUCTION ONLY. Also delegates
      to production_service.update_header() for the same reason.
    A caller must leave the fields that don't apply to its source_type at
    their default of None — enforced below, not just by convention.

    Returns (record, summary) where summary has "added_lines",
    "removed_lines", and "changed_lines" — exactly what the audit entry
    also captures, so a route/frontend can build a before/after preview
    from the same data without a second query.
    """
    if source_type not in _REGISTRY:
        raise RecordCorrectionError(f"unknown record type: {source_type}")
    if not reason:
        raise RecordCorrectionError("A correction reason is required")
    if not lines:
        raise RecordCorrectionError("A correction needs at least one product line")
    changing_recipient = customer_id is not None or new_customer_name is not None or sales_category_id is not None
    if changing_recipient and source_type != "dispatch":
        raise RecordCorrectionError(f"recipient correction is not supported for source type: {source_type}")
    changing_returned_by = returned_by_customer_id is not None or returned_by_name is not None or signed_by_name is not None
    if changing_returned_by and source_type != "returns":
        raise RecordCorrectionError(f"returned-by/signer correction is not supported for source type: {source_type}")
    if shift is not None and source_type != "production":
        raise RecordCorrectionError(f"shift correction is not supported for source type: {source_type}")

    cfg = _REGISTRY[source_type]
    record = db.session.get(cfg["model"], record_id)
    if record is None:
        raise RecordCorrectionError("record not found")

    was_void = record.status == STATUS_VOID
    if was_void and via_request_id is None:
        # Full targeted Operator correction/void/requests/notification
        # package, section 1: a voided record can still be corrected —
        # but ONLY via an approved, consumed one-time Request Correction
        # grant (via_request_id set by the /correct route below), never
        # by a direct edit. This is the one narrow exception to the
        # otherwise-unconditional "void is permanent" refusal.
        raise RecordCorrectionError(
            "A voided record cannot be edited directly — submit a Request Correction; "
            "once a Manager or Super Administrator approves it, you may correct this "
            "record's details while it remains void"
        )

    if expected_updated_at is not None:
        current = record.updated_at.isoformat() if record.updated_at else None
        if current != expected_updated_at:
            raise RecordCorrectionConflict("This record changed while you were reviewing it. Reload and try again.")

    error_cls = cfg["error"]
    was_finalized = record.status == STATUS_FINALIZED
    date_before = record.date

    # Correcting a voided record must NEVER unvoid it (section 1/17): no
    # status change to Finalized, no restored stock contribution, no
    # compensating adjustment. update_header()/update_recipient() (each
    # source book's own field-level guard) and finalize() all require
    # STATUS_DRAFT — exactly like the reopen-edit-refinalize dance just
    # below already does for a FINALIZED record, this temporarily flips
    # status to draft so those same, unmodified functions run completely
    # unchanged, then restores status=void (and the ORIGINAL voided_by/
    # voided_at/void_reason — never a new void event, never a fresh
    # timestamp) once the edit is done. This is safe for stock: every
    # Issued/Returns/Production aggregate query filters status ==
    # FINALIZED only (see stock_service.py) — a record sitting at
    # STATUS_DRAFT for the middle of this one never-separately-committed
    # transaction can never be read, counted, or double-counted by
    # anything else.
    void_provenance = None
    if was_void:
        void_provenance = {
            "voided_by": record.voided_by, "voided_at": record.voided_at, "void_reason": record.void_reason,
        }
        record.status = STATUS_DRAFT

    if was_finalized:
        cfg["reopen"](record, actor, f"Correction: {reason}")

    # Header fields (date + whatever else applies to this source_type) —
    # Returns/Production delegate entirely to their existing update_header()
    # (the same function the draft-editing endpoints already use; reachable
    # here because the reopen above already guarantees draft status), so
    # notes/remarks is applied there too rather than via the generic
    # setattr below. Dispatch has no equivalent generic update_header(), so
    # it keeps its existing direct-field-plus-update_recipient() path.
    if source_type == "returns":
        try:
            returns_service.update_header(
                record, date=date, returned_by_customer_id=returned_by_customer_id,
                returned_by_name=returned_by_name, signed_by_name=signed_by_name,
                remarks=notes, user=actor,
            )
        except returns_service.ReturnError as e:
            raise error_cls(str(e)) from e
    elif source_type == "production":
        try:
            production_service.update_header(record, date=date, shift=shift, remarks=notes, user=actor)
        except production_service.ProductionError as e:
            raise error_cls(str(e)) from e
    else:
        if date is not None:
            record.date = date
        if changing_recipient:
            try:
                dispatch_service.update_recipient(
                    record, customer_id=customer_id, new_customer_name=new_customer_name,
                    sales_category_id=sales_category_id, user=actor,
                )
            except dispatch_service.DispatchError as e:
                raise error_cls(str(e)) from e

    existing_by_id = {line.id: line for line in record.lines}
    keep_ids = set()
    added_lines = []
    changed_lines = []
    for entry in lines:
        built = cfg["build_line"](
            entry.get("product_id"), entry.get("cartons", 0), entry.get("packs", 0), entry.get("pieces", 0),
            line_notes=entry.get("line_notes"),
        )
        line_id = entry.get("id")
        existing = existing_by_id.get(line_id) if line_id is not None else None
        if existing is not None:
            before_line = {
                "product_id": existing.product_id, "cartons": existing.cartons,
                "packs": existing.packs, "pieces": existing.pieces, "line_notes": existing.line_notes,
            }
            for key, value in built.items():
                setattr(existing, key, value)
            keep_ids.add(line_id)
            after_line = {k: built[k] for k in before_line}
            if before_line != after_line:
                changed_lines.append({"line_id": line_id, "before": before_line, "after": built})
        else:
            new_line = cfg["line_model"](**built)
            # record.lines.append(...), not db.session.add(...) directly —
            # the relationship keeps the in-memory collection correctly
            # synchronized (and still adds the row to the session via
            # cascade), which the "at least one line remains" check right
            # below depends on. A direct session.add() would persist the
            # row but leave record.lines looking empty until the next
            # fresh query, exactly the same class of staleness bug as
            # daily_reset_service's line removal (see its comment there).
            record.lines.append(new_line)
            added_lines.append(built)

    removed_lines = []
    for line_id, line in list(existing_by_id.items()):
        if line_id not in keep_ids:
            removed_lines.append({
                "line_id": line_id, "product_id": line.product_id,
                "cartons": line.cartons, "packs": line.packs, "pieces": line.pieces,
            })
            record.lines.remove(line)

    # Returns/Production already applied notes/remarks via update_header()
    # above — only Dispatch (no update_header() of its own) still needs the
    # generic setattr here.
    if source_type == "dispatch" and notes is not None:
        setattr(record, cfg["notes_field"], notes)
    record.updated_by = actor.id
    db.session.flush()

    if not record.lines:
        raise error_cls("A correction cannot leave a record with no product lines")

    if was_finalized:
        cfg["finalize"](record, actor)
    elif was_void:
        # Restore exactly the original void event — never a new one:
        # same voider, same original timestamp, same original reason.
        record.status = STATUS_VOID
        record.voided_by = void_provenance["voided_by"]
        record.voided_at = void_provenance["voided_at"]
        record.void_reason = void_provenance["void_reason"]
    product_usage_service.record_usage(source_type, record.id, {line.product_id for line in record.lines})

    record_audit(
        actor, "correct_record", cfg["audit_entity_type"], entity_id=record.id,
        before={
            "reason": reason, "actor_role": actor.role,
            "status_before": STATUS_FINALIZED if was_finalized else (STATUS_VOID if was_void else record.status),
            "date_before": date_before,
        },
        after={
            "reason": reason, "actor_role": actor.role, "status_after": record.status,
            "added_lines": added_lines, "removed_lines": removed_lines, "changed_lines": changed_lines,
            "notes": getattr(record, cfg["notes_field"]) if notes is not None else None,
            "date_after": record.date,
            # Section 21: distinguishes an Operator's direct same-24h-window
            # edit from one made under a Manager/Super-Admin-approved
            # historical correction grant — via_request_id links straight
            # back to the CorrectionRequest row that authorized it.
            "via_request_id": via_request_id,
            "correction_source": "approved_grant" if via_request_id is not None else "direct",
            # Section 1: proves this correction never unvoided the record —
            # explicit even though status_after already shows it.
            "void_status_preserved": was_void,
        },
    )
    return record, {"added_lines": added_lines, "removed_lines": removed_lines, "changed_lines": changed_lines}


def audit_history(source_type, record_id):
    """Chronological correction/reopen/void/finalize trail for one record
    — read-only, used by the History detail view. Reuses AuditLog rather
    than a bespoke per-record history table."""
    if source_type not in _REGISTRY:
        raise RecordCorrectionError(f"unknown record type: {source_type}")
    from webapp.models.audit_log import AuditLog
    entity_type = _REGISTRY[source_type]["audit_entity_type"]
    entries = (
        AuditLog.query
        .filter(AuditLog.entity_type == entity_type, AuditLog.entity_id == str(record_id))
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .all()
    )
    return [e.to_dict() for e in entries]
