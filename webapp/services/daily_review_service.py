"""
Final pre-deployment correction — Manager/Super Administrator Daily
Figures review-and-submit workflow. See
webapp/models/daily_review_session.py for the data-model rationale.

This service never touches DailyFigure quantities, never creates source-
book movement, and never duplicates a business record — it only manages
the review session's own metadata (webapp.services.stock_service remains
the sole place Opening Stock is actually written, via the existing
upsert_daily_figure(), unchanged by this feature). "Reviewed"/"Edited"/
"Skipped" here describes review PROGRESS, not stock data.
"""
from datetime import datetime, timezone

from webapp.extensions import db
from webapp.models.daily_review_session import (
    REVIEW_PRODUCT_STATE_EDITED,
    REVIEW_PRODUCT_STATE_REVIEWED,
    REVIEW_PRODUCT_STATE_SKIPPED,
    REVIEW_PRODUCT_STATES,
    REVIEW_PRODUCT_STATES_COUNTED_AS_REVIEWED,
    REVIEW_STATUS_IN_PROGRESS,
    REVIEW_STATUS_SUBMITTED,
    REVIEW_STATUS_REOPENED,
    DailyReviewProductState,
    DailyReviewSession,
)
from webapp.models.daily_figure import DailyFigure
from webapp.models.dispatch import SHIFT_DAY
from webapp.models.product import Product
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import daily_entry_status_service, stock_service
from webapp.services.audit_service import record_audit


class ReviewError(ValueError):
    """User-facing validation problem — mapped to HTTP 400 by routes."""


class ReviewConflict(ValueError):
    """A concurrency/ownership conflict — mapped to HTTP 409 by routes."""


class ReviewConfirmationRequired(ValueError):
    """
    Raised by submit_review() when nothing hard-blocks submission but at
    least one product is still genuinely unreviewed — mapped to HTTP 409
    by routes, carrying `unreviewed_count` so the frontend can show
    "N products have not been reviewed. Submit anyway?" and, if the
    Manager/Super Admin confirms, resend the same submit call with
    force=True. Never raised at all when force=True was already passed,
    and never raised for a genuine hard blocker (see build_summary()'s
    `blocking_count` vs `unreviewed_count` split) — that always raises
    plain ReviewError instead, which force can never bypass.
    """
    def __init__(self, unreviewed_count):
        self.unreviewed_count = unreviewed_count
        super().__init__(f"{unreviewed_count} product(s) have not been reviewed. Submit anyway?")


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _require_role(user):
    if user.role not in (ROLE_MANAGER, ROLE_SUPER_ADMIN):
        raise ReviewError("Only a Manager or Super Administrator may use the Daily Figures review workflow")


def _user_label(user_id):
    """Mirrors daily_entry_status_service._user_label() — small enough
    (and this module's dependency graph clean enough) that duplicating it
    beats introducing a shared-utils module for one four-line helper."""
    if user_id is None:
        return None
    from webapp.models.user import User
    user = db.session.get(User, user_id)
    return user.username if user else "a former user"


def _session_dict(session):
    d = session.to_dict()
    d["started_by_username"] = _user_label(session.started_by)
    d["submitted_by_username"] = _user_label(session.submitted_by)
    d["reopened_by_username"] = _user_label(session.reopened_by)
    return d


def _eligible_products():
    """The same 'active products' set Daily Figures, Reset Daily Values,
    and product ranking all already treat as the working catalogue —
    never a separate/competing product list."""
    return Product.query.filter_by(active=True).order_by(Product.display_order, Product.name).all()


def get_session(date, shift):
    return DailyReviewSession.query.filter_by(date=date, shift=shift).first()


def session_view(date, shift):
    session = get_session(date, shift)
    return _session_dict(session) if session else None


def _get_or_create_open_session(date, shift, user):
    session = get_session(date, shift)
    if session is None:
        session = DailyReviewSession(
            date=date, shift=shift, status=REVIEW_STATUS_IN_PROGRESS,
            started_by=user.id, started_role=user.role,
        )
        db.session.add(session)
        db.session.flush()
        return session
    if session.status == REVIEW_STATUS_SUBMITTED:
        raise ReviewConflict(
            "This review has already been submitted — reopen it first to make changes."
        )
    return session


def mark_product_state(date, shift, product_id, user, state, reason=None):
    """Records that this reviewer has visited (and either left unchanged,
    corrected, or deliberately deferred) this one product during the
    current review session — never a stock write, never a completion
    claim, never an audit event claiming submission. Auto-creates the
    session (in_progress) on first use; raises ReviewConflict if the
    session is already submitted (must be reopened first).

    `reason` is only ever stored for state=="edited" (a correction
    reason — see section 12's "require a short correction reason" —
    enforced at SUBMISSION time by build_summary()/submit_review(), not
    here, since the reason may legitimately be supplied later by
    revisiting the product before the review is submitted)."""
    _require_role(user)
    if state not in REVIEW_PRODUCT_STATES:
        raise ReviewError(f"state must be one of {REVIEW_PRODUCT_STATES}")

    product = db.session.get(Product, product_id)
    if product is None:
        raise ReviewError("product does not exist")

    session = _get_or_create_open_session(date, shift, user)

    row = DailyReviewProductState.query.filter_by(review_session_id=session.id, product_id=product_id).first()
    if row is None:
        row = DailyReviewProductState(review_session_id=session.id, product_id=product_id)
        db.session.add(row)
    row.state = state
    if state == REVIEW_PRODUCT_STATE_EDITED:
        row.reason = reason or row.reason  # never blank out a previously-supplied reason with an empty resubmit
    else:
        row.reason = None
    row.updated_by = user.id
    # Safety correction — snapshot the DailyFigure row's current updated_at
    # (None if it doesn't exist yet) fresh on every mark, not just once:
    # this is what lets a re-review after a concurrent Opening Stock/notes
    # change correctly clear the staleness flag on the next build_summary()
    # call — see _daily_figure_changed_since() below.
    figure = DailyFigure.query.filter_by(product_id=product_id, date=date, shift=shift).first()
    row.daily_figure_updated_at = figure.updated_at if figure is not None else None
    # Explicit, not relied-upon-via-onupdate: a re-review after a source
    # correction often sets state/updated_by to the SAME values they
    # already had, so SQLAlchemy detects no net change and skips emitting
    # an UPDATE at all — silently leaving onupdate=_utcnow from firing and
    # updated_at stale (older than the source record's own updated_at),
    # which would make _source_touched_since() keep reporting "changed
    # since review" forever even after the reviewer re-reviewed it.
    row.updated_at = _utcnow()
    db.session.flush()
    return row


def mark_reviewed(date, shift, product_id, user, edited=False, reason=None):
    state = REVIEW_PRODUCT_STATE_EDITED if edited else REVIEW_PRODUCT_STATE_REVIEWED
    return mark_product_state(date, shift, product_id, user, state, reason=reason)


def mark_skipped(date, shift, product_id, user):
    return mark_product_state(date, shift, product_id, user, REVIEW_PRODUCT_STATE_SKIPPED)


def _source_touched_since(product_id, date, shift, since):
    """True if any finalized-or-draft Dispatch/Returns/Production record
    touching this exact product/date/shift has been updated (created,
    corrected, reopened, voided — anything that bumps its own updated_at)
    since `since` — the concurrency check behind "source record changed
    during review" (section 17/21): a Correct Record edit to the
    underlying source book must demote an already-reviewed product back
    to needing review, even though Daily Figures itself derives live and
    never stores a stale value."""
    if since is None:
        return False
    from webapp.models.dispatch import Dispatch, DispatchLine
    from webapp.models.production_record import ProductionLine, ProductionRecord
    from webapp.models.return_record import ReturnLine, ReturnRecord

    if shift == SHIFT_DAY:
        dispatch_touched = db.session.query(
            db.session.query(DispatchLine.id)
            .join(Dispatch, Dispatch.id == DispatchLine.dispatch_id)
            .filter(
                Dispatch.date == date, Dispatch.shift == SHIFT_DAY,
                DispatchLine.product_id == product_id, Dispatch.updated_at > since,
            )
            .exists()
        ).scalar()
        if dispatch_touched:
            return True

        returns_touched = db.session.query(
            db.session.query(ReturnLine.id)
            .join(ReturnRecord, ReturnRecord.id == ReturnLine.return_id)
            .filter(
                ReturnRecord.date == date, ReturnLine.product_id == product_id,
                ReturnRecord.updated_at > since,
            )
            .exists()
        ).scalar()
        if returns_touched:
            return True

    production_touched = db.session.query(
        db.session.query(ProductionLine.id)
        .join(ProductionRecord, ProductionRecord.id == ProductionLine.production_id)
        .filter(
            ProductionRecord.date == date, ProductionRecord.shift == shift,
            ProductionLine.product_id == product_id, ProductionRecord.updated_at > since,
        )
        .exists()
    ).scalar()
    return bool(production_touched)


def _daily_figure_changed_since(product_id, date, shift, since):
    """Safety correction, item 1 — True if the DailyFigure row's own
    updated_at (Opening Stock, notes, provenance — the manual fields this
    review workflow itself may correct) no longer matches the snapshot
    taken when this product was last marked reviewed. Deliberately an
    equality check, not a '>' comparison like _source_touched_since():
    `since` is the exact value seen at review time (or None if no row
    existed yet), so ANY difference — a genuinely later write, or a row
    now existing where none did — means the record diverged from what the
    reviewer looked at. daily_figure_view() never writes to the database
    on a read, so simply viewing/paging through a product during review
    can never trip this — only an actual save via upsert_daily_figure()
    (a real Opening Stock/notes change) can."""
    figure = DailyFigure.query.filter_by(product_id=product_id, date=date, shift=shift).first()
    current = figure.updated_at if figure is not None else None
    return current != since


def build_summary(date, shift):
    """The final review screen's data — every eligible product's review
    state, entry-ownership status, live Daily Figures values, and whether
    it currently blocks final submission. Never mutates anything, safe to
    call on every screen load/refresh."""
    session = get_session(date, shift)
    products = _eligible_products()

    states_by_product = {}
    if session is not None:
        rows = DailyReviewProductState.query.filter_by(review_session_id=session.id).all()
        states_by_product = {r.product_id: r for r in rows}

    entries = []
    reviewed_count = 0
    skipped_count = 0
    blocking_count = 0
    unreviewed_count = 0
    for product in products:
        review_row = states_by_product.get(product.id)
        review_state = review_row.state if review_row else "not_reviewed"

        entry_status = daily_entry_status_service.status_view(date, shift, product.id)
        locked_by_other = entry_status["locked_by"] is not None

        rule = product.current_packaging_rule()
        validation_error = None if rule else "No packaging rule configured for this product yet"

        view = stock_service.daily_figure_view(product, date, shift) if rule else None

        source_changed = False
        figure_changed = False
        if review_row is not None and review_state in REVIEW_PRODUCT_STATES_COUNTED_AS_REVIEWED:
            source_changed = _source_touched_since(product.id, date, shift, review_row.updated_at)
            figure_changed = _daily_figure_changed_since(product.id, date, shift, review_row.daily_figure_updated_at)
        stale = source_changed or figure_changed

        # Section 12: an edited product must have a correction reason
        # before the REVIEW can be submitted — never required merely to
        # move past it during navigation, only to finally submit.
        missing_reason = (
            review_state == REVIEW_PRODUCT_STATE_EDITED
            and review_row is not None
            and not review_row.reason
        )

        counted_as_reviewed = (
            review_state in REVIEW_PRODUCT_STATES_COUNTED_AS_REVIEWED and not stale and not missing_reason
        )
        # Final Manager/Super Admin submission-flow correction — "blocks"
        # is now reserved for genuine integrity hazards that can NEVER be
        # bypassed: another user's live lock, a missing packaging rule, an
        # edit saved without its required correction reason, or a source/
        # DailyFigure record that changed since it was reviewed. A product
        # that is simply "not_reviewed" or "skipped" — nothing else wrong
        # with it — is no longer counted here; see `unreviewed` below,
        # which Manager/Super Admin MAY explicitly submit past (with
        # confirmation) via submit_review(..., force=True). This never
        # weakens the hard blockers themselves — those still refuse
        # submission unconditionally, force or not.
        blocks = locked_by_other or bool(validation_error) or missing_reason or stale
        unreviewed = (not counted_as_reviewed) and not blocks
        if blocks:
            blocking_count += 1
        elif unreviewed:
            unreviewed_count += 1
        if counted_as_reviewed:
            reviewed_count += 1
        if review_state == REVIEW_PRODUCT_STATE_SKIPPED:
            skipped_count += 1

        entries.append({
            "product_id": product.id,
            "product_name": product.name,
            "review_state": "not_reviewed" if stale else review_state,
            "source_changed_since_review": source_changed,
            "daily_figure_changed_since_review": figure_changed,
            "correction_reason": review_row.reason if review_row else None,
            "missing_correction_reason": missing_reason,
            "entry_status": entry_status["status"],
            "completion_type": entry_status["completion_type"],
            "completed_by": entry_status["completed_by"],
            "completed_at": entry_status["completed_at"],
            "locked_by": entry_status["locked_by"],
            "validation_error": validation_error,
            "blocks_submission": blocks,
            "unreviewed": unreviewed,
            "view": view,
        })

    return {
        "date": date, "shift": shift,
        "session": _session_dict(session) if session else None,
        "products": entries,
        "total": len(products),
        "reviewed_count": reviewed_count,
        "skipped_count": skipped_count,
        "blocking_count": blocking_count,
        "unreviewed_count": unreviewed_count,
        "can_submit": (
            session is not None and session.status != REVIEW_STATUS_SUBMITTED and blocking_count == 0 and len(products) > 0
        ),
    }


def submit_review(date, shift, user, force=False):
    """Transactional: the ONLY action that marks the review submitted, and
    the only thing it writes is this session row's own status fields —
    per-product review rows are never mutated here (an unreviewed product
    stays exactly as unreviewed/skipped after a force=True submission as
    it was before — never auto-marked reviewed, never given a fabricated
    value, never turned into a correction or a new Opening anchor), so
    there is no multi-row write to partially apply. Recomputes eligibility
    live (never trusts a client-supplied "everything is fine" flag).

    Two independent gates, in order:
      1. blocking_count (locks/validation errors/missing correction
         reasons/staleness-since-review) — always refused, force or not.
      2. unreviewed_count (genuinely never-reviewed or skipped products,
         nothing else wrong) — refused UNLESS force=True, in which case
         the Manager/Super Administrator has already seen and explicitly
         confirmed a "N products have not been reviewed. Submit anyway?"
         prompt (see routes/daily_review.py's submit() and static/
         index.html's submitBtn handler).
    """
    _require_role(user)
    session = get_session(date, shift)
    if session is None:
        raise ReviewError("No review is in progress for this date and shift yet — review at least one product first.")
    if session.status == REVIEW_STATUS_SUBMITTED:
        raise ReviewError("This review has already been submitted.")

    summary = build_summary(date, shift)
    if summary["blocking_count"] > 0:
        raise ReviewError(f"{summary['blocking_count']} product(s) still require review before submission.")
    if summary["unreviewed_count"] > 0 and not force:
        raise ReviewConfirmationRequired(summary["unreviewed_count"])

    before = session.to_dict()
    session.status = REVIEW_STATUS_SUBMITTED
    session.submitted_by = user.id
    session.submitted_role = user.role
    session.submitted_at = _utcnow()
    db.session.flush()

    record_audit(
        user, "submit_review", "daily_review_session", entity_id=f"{date}|{shift}",
        before=before,
        after={**session.to_dict(), "unreviewed_count_at_submit": summary["unreviewed_count"]},
    )
    return session


def reopen_review(date, shift, user, reason):
    _require_role(user)
    session = get_session(date, shift)
    if session is None:
        raise ReviewError("No review exists for this date and shift yet.")
    if session.status != REVIEW_STATUS_SUBMITTED:
        raise ReviewError("Only a submitted review can be reopened.")
    if not reason:
        raise ReviewError("A reason is required to reopen a submitted review.")

    before = session.to_dict()
    session.status = REVIEW_STATUS_REOPENED
    session.reopened_by = user.id
    session.reopened_at = _utcnow()
    session.reopen_reason = reason
    db.session.flush()

    record_audit(
        user, "reopen_review", "daily_review_session", entity_id=f"{date}|{shift}",
        before=before, after=session.to_dict(),
    )
    return session


def preview_review_state(date, shift, product_ids):
    """Safety correction, item 2 — read-only: the current review session's
    status (if any) and each of the given products' current review state,
    for Reset Daily Values' own preview to show before a Manager/Super
    Administrator confirms a reset. Never mutates anything, and never
    creates a session merely by being called (unlike mark_product_state()'s
    _get_or_create_open_session())."""
    session = get_session(date, shift)
    if session is None:
        return {"session_status": None, "product_states": {pid: "not_reviewed" for pid in product_ids}}
    rows = DailyReviewProductState.query.filter(
        DailyReviewProductState.review_session_id == session.id,
        DailyReviewProductState.product_id.in_(product_ids),
    ).all()
    found = {row.product_id: row.state for row in rows}
    return {
        "session_status": session.status,
        "product_states": {pid: found.get(pid, "not_reviewed") for pid in product_ids},
    }


def clear_product_states_for_reset(date, shift, product_ids, actor, reason, reset_mode):
    """Safety correction, item 2 — called by daily_reset_service.execute()
    as part of the SAME database transaction as the rest of a reset (this
    function only ever flushes, never commits — the caller's route commits
    once at the end, so a failure anywhere in the reset rolls this back
    too). If no review has ever been started for this date+shift, this is
    a deliberate no-op: a reset must never spontaneously create a review
    session (see the module docstring's "never a duplicate business
    record" rule, mirrored here for review sessions).

    Clears (deletes) the DailyReviewProductState row for each product in
    `product_ids` that has one, in this session — the absence of a row
    already means "not yet reviewed" everywhere else in this module, so
    deleting is the correct way to force these specific products back to
    that state without inventing a fourth state value. Products NOT in
    `product_ids` (e.g. a one-product reset) are left completely alone —
    their review state survives exactly as the spec requires.

    If the session was submitted, it is demoted to reopened (NEVER a new
    session row — the existing unique constraint on (date, shift) means
    there is nothing else it safely could be) so the interface stops
    claiming the period is submitted once its values have been reset out
    from under that submission. An in-progress or already-reopened
    session's status is left as-is; only its cleared products' states
    change.

    Writes its own dedicated audit event (only when something actually
    changed) — record_audit() already captures actor/timestamp; the
    before/after payloads here carry the review-specific fields section 3
    of the safety correction spec calls for: review session id, previous
    and new review status, which product states were cleared, the reset
    mode, and the reset's own reason (date/shift are on the entity_id).
    """
    session = get_session(date, shift)
    if session is None:
        return None

    before_status = session.status
    if session.status == REVIEW_STATUS_SUBMITTED:
        session.status = REVIEW_STATUS_REOPENED
        session.reopened_by = actor.id
        session.reopened_at = _utcnow()
        session.reopen_reason = f"Reopened automatically by Reset Daily Values ({reset_mode}): {reason}"

    rows = DailyReviewProductState.query.filter(
        DailyReviewProductState.review_session_id == session.id,
        DailyReviewProductState.product_id.in_(product_ids),
    ).all()
    cleared = [{"product_id": row.product_id, "previous_state": row.state} for row in rows]
    for row in rows:
        db.session.delete(row)

    db.session.flush()

    if before_status != session.status or cleared:
        record_audit(
            actor, "reset_review_state", "daily_review_session", entity_id=f"{date}|{shift}",
            before={
                "review_session_id": session.id,
                "previous_review_status": before_status,
                "date": date, "shift": shift,
            },
            after={
                "review_session_id": session.id,
                "previous_review_status": before_status,
                "new_review_status": session.status,
                "cleared_product_states": cleared,
                "reset_mode": reset_mode,
                "reason": reason,
                "date": date, "shift": shift,
            },
        )

    return {
        "review_session_id": session.id,
        "previous_status": before_status,
        "new_status": session.status,
        "cleared_product_states": cleared,
    }
