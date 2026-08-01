"""
Ownership-lock and completion tracking for one Date + Shift + Product Daily
Figures entry (Stage 7). Prevents two Operators from working on or
finalizing the same entry concurrently, and carries the "No Activity
Today" completion (zero movement is a valid, reviewed outcome, not an
unreviewed blank).

Concurrency safety: claim_completion() uses a compare-and-swap UPDATE
(`WHERE completed = 0`, checking the affected row count) rather than a
plain "check then write" — the same pattern this app already uses for
feature-flag cascade changes. Under SQLite's single-writer transaction
model this is enough to guarantee that of two simultaneous completion
attempts for the same row, exactly one succeeds and the other reliably
observes the conflict, with no duplicate DailyFigure row and no double
count either way.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from webapp.extensions import db
from webapp.models.daily_entry_status import (
    COMPLETION_TYPE_NO_ACTIVITY, COMPLETION_TYPE_WITH_DATA, DailyEntryStatus,
)
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services.audit_service import record_audit

# A lock this old is treated as abandoned (e.g. the Operator's browser tab
# crashed, or they walked away) rather than as a live conflict — an
# authorized takeover is still always available sooner via Manager/Super
# Administrator, but an ordinary Operator simply reopening the same page
# after a break must not be permanently blocked by their own earlier tab.
LOCK_STALE_MINUTES = 15


class DailyEntryStatusError(ValueError):
    """User-facing validation problem — mapped to HTTP 400 by routes."""


class DailyEntryStatusConflict(ValueError):
    """Another user already owns or completed this entry — mapped to HTTP 409 by routes."""


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_stale(lock_acquired_at):
    if lock_acquired_at is None:
        return True
    return _utcnow() - lock_acquired_at > timedelta(minutes=LOCK_STALE_MINUTES)


def _format_when(dt):
    """'%-d' (no leading zero) is a glibc-only strftime extension that
    raises ValueError on Windows — build the no-leading-zero day number
    directly instead, so this works identically on every platform."""
    if dt is None:
        return ""
    return f"{dt.day} {dt.strftime('%b %Y %H:%M')}"


def get_or_create(date, shift, product_id):
    """Race-safe get-or-create against the (date, shift, product_id)
    unique constraint — two simultaneous first-touches of the same brand
    new entry are resolved by letting the database's own constraint decide
    the winner, never by a plain check-then-insert."""
    row = DailyEntryStatus.query.filter_by(date=date, shift=shift, product_id=product_id).first()
    if row is not None:
        return row
    row = DailyEntryStatus(date=date, shift=shift, product_id=product_id, completed=False)
    db.session.add(row)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        row = DailyEntryStatus.query.filter_by(date=date, shift=shift, product_id=product_id).first()
    return row


def _user_label(user_id):
    if user_id is None:
        return None
    from webapp.models.user import User
    user = db.session.get(User, user_id)
    return user.username if user else "a former user"


def status_view(date, shift, product_id):
    """Read-only summary for the frontend — one of:
    'completed' (with_data or no_activity — see completion_type),
    'in_progress' (an active, non-stale lock is held),
    'not_started' (nothing recorded yet).
    Never mutates anything, so safe to call on every page render."""
    row = DailyEntryStatus.query.filter_by(date=date, shift=shift, product_id=product_id).first()
    if row is None:
        return {
            "status": "not_started", "completed": False, "completion_type": None,
            "completed_by": None, "completed_at": None,
            "locked_by": None, "locked_at": None,
            "reopened_by": None, "reopened_at": None, "reopen_reason": None,
        }

    locked_by = None
    locked_at = None
    if row.lock_user_id is not None and not row.completed and not _is_stale(row.lock_acquired_at):
        locked_by = _user_label(row.lock_user_id)
        locked_at = row.lock_acquired_at.isoformat() if row.lock_acquired_at else None

    if row.completed:
        status = "completed"
    elif locked_by:
        status = "in_progress"
    else:
        status = "not_started"

    return {
        "status": status, "completed": row.completed, "completion_type": row.completion_type,
        "completed_by": _user_label(row.completed_by), "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "locked_by": locked_by, "locked_at": locked_at,
        "reopened_by": _user_label(row.reopened_by), "reopened_at": row.reopened_at.isoformat() if row.reopened_at else None,
        "reopen_reason": row.reopen_reason,
    }


def check_operator_conflict(date, shift, product_id, user):
    """Returns a friendly conflict message if `user` (an Operator) must not
    proceed right now, else None. Never mutates. Manager/Super Admin never
    call this — their correction authority (Stage 7 section 3) bypasses
    ownership locking entirely."""
    row = DailyEntryStatus.query.filter_by(date=date, shift=shift, product_id=product_id).first()
    if row is None:
        return None
    if row.completed:
        who = _user_label(row.completed_by) or "another user"
        when = _format_when(row.completed_at)
        return f"Already inputted by {who} at {when}." if when else f"Already inputted by {who}."
    if row.lock_user_id is not None and row.lock_user_id != user.id and not _is_stale(row.lock_acquired_at):
        who = _user_label(row.lock_user_id) or "another user"
        return f"This product is currently being entered by {who}."
    return None


def acquire_lock(date, shift, product_id, user):
    """Operator (or any role) starts/refreshes work on this entry. Raises
    DailyEntryStatusConflict if someone else already owns a live lock or
    has already completed it."""
    row = get_or_create(date, shift, product_id)
    conflict = check_operator_conflict(date, shift, product_id, user)
    if conflict:
        raise DailyEntryStatusConflict(conflict)
    row.lock_user_id = user.id
    row.lock_acquired_at = _utcnow()
    db.session.flush()
    return row


def release_lock(date, shift, product_id, user):
    """A user voluntarily leaving the entry without completing it (e.g.
    navigating away) — only ever releases a lock they themselves hold, so
    one Operator can never clear another's in-progress work this way."""
    row = DailyEntryStatus.query.filter_by(date=date, shift=shift, product_id=product_id).first()
    if row is None or row.lock_user_id != user.id:
        return
    row.lock_user_id = None
    row.lock_acquired_at = None
    db.session.flush()


def claim_completion(date, shift, product_id, user, completion_type):
    """
    The one concurrency-safe gate every completion path (a real Opening
    Stock save, or an explicit No Activity Today) must go through before
    any data is written. A conditional UPDATE ... WHERE completed = 0,
    checked by affected-row-count, is the actual guarantee: of two
    simultaneous callers for the same row, exactly one flips `completed`
    to true and proceeds; the other's UPDATE matches zero rows and raises
    a conflict — no window where both believe they succeeded.

    Manager/Super Administrator corrections to an ALREADY-completed entry
    intentionally do not call this at all (see webapp/routes/daily_figures.py) —
    their correction authority bypasses the claim, and the original
    completed_by/completed_at attribution is preserved untouched so
    "Already inputted by X at Y" keeps naming the original Operator even
    after a Manager corrects the underlying figures.
    """
    row = get_or_create(date, shift, product_id)
    if row.completed:
        who = _user_label(row.completed_by) or "another user"
        when = _format_when(row.completed_at)
        message = f"Already inputted by {who} at {when}." if when else f"Already inputted by {who}."
        raise DailyEntryStatusConflict(message)

    now = _utcnow()
    result = db.session.execute(
        update(DailyEntryStatus)
        .where(DailyEntryStatus.id == row.id, DailyEntryStatus.completed.is_(False))
        .values(
            completed=True, completion_type=completion_type,
            completed_by=user.id, completed_at=now,
            lock_user_id=None, lock_acquired_at=None,
        )
    )
    if result.rowcount == 0:
        # Someone else's claim committed between our SELECT above and this
        # UPDATE — re-read their attribution for the message.
        db.session.expire(row)
        who = _user_label(row.completed_by) or "another user"
        when = _format_when(row.completed_at)
        message = f"Already inputted by {who} at {when}." if when else f"Already inputted by {who}."
        raise DailyEntryStatusConflict(message)

    db.session.flush()
    db.session.refresh(row)
    return row


def mark_no_activity(date, shift, product_id, user):
    """Explicit 'No Activity Today' completion — never touches Opening
    Stock, never creates a Dispatch/Returns/Production movement, and (like
    every completion) is only ever reachable if the entry isn't already
    completed, via claim_completion()'s compare-and-swap guarantee."""
    row = claim_completion(date, shift, product_id, user, COMPLETION_TYPE_NO_ACTIVITY)
    record_audit(
        user, "no_activity_today", "daily_entry_status",
        entity_id=f"{date}|{shift}|{product_id}",
        after={"completion_type": COMPLETION_TYPE_NO_ACTIVITY, "completed_by": user.id},
    )
    return row


def mark_completed_with_data(date, shift, product_id, user):
    """Called after a successful Operator Opening Stock save — the normal
    'real data' completion path. See claim_completion() for the
    concurrency guarantee this relies on. Raises DailyEntryStatusConflict
    if someone else already completed it first."""
    return claim_completion(date, shift, product_id, user, COMPLETION_TYPE_WITH_DATA)


def mark_completed_with_data_if_not_already(date, shift, product_id, user):
    """Manager/Super Administrator variant used by the Daily Figures
    upsert route (webapp/routes/daily_figures.py): their correction
    authority (Stage 7 section 3) bypasses ownership locking entirely, so
    an already-completed entry is left exactly as attributed — never an
    error, and never rewrites who originally completed it. Only claims
    completion (crediting this actor) when nothing was completed yet,
    e.g. a Manager performing a product's very first Opening Stock entry
    themselves."""
    row = DailyEntryStatus.query.filter_by(date=date, shift=shift, product_id=product_id).first()
    if row is not None and row.completed:
        return row
    try:
        return claim_completion(date, shift, product_id, user, COMPLETION_TYPE_WITH_DATA)
    except DailyEntryStatusConflict:
        # Lost a genuine race to someone else's claim in between — their
        # completion stands; this elevated-role write itself still
        # succeeds (the caller already wrote the corrected DailyFigure).
        return get_or_create(date, shift, product_id)


def bucket_for_date_shift(date, shift, product_ids):
    """
    One query, not one per product (final pre-deployment correction,
    Daily Figures usage-ranked ordering): returns {product_id: N} where
    N is 0 (not started), 1 (in progress — a live, non-stale lock is
    held), or 2 (completed — real data or No Activity Today) for this
    exact date+shift. A product_id with no row at all is 0 implicitly
    (via .get(..., 0) at the call site) — nothing to query for a product
    that was never touched on this date+shift.

    This is purely a read — it never creates a row, never claims a lock,
    and is safe to call on every Daily Figures list load.
    """
    if not product_ids:
        return {}
    rows = DailyEntryStatus.query.filter(
        DailyEntryStatus.date == date,
        DailyEntryStatus.shift == shift,
        DailyEntryStatus.product_id.in_(product_ids),
    ).all()
    buckets = {}
    for row in rows:
        if row.completed:
            buckets[row.product_id] = 2
        elif row.lock_user_id is not None and not _is_stale(row.lock_acquired_at):
            buckets[row.product_id] = 1
        else:
            buckets[row.product_id] = 0
    return buckets


def takeover_lock(date, shift, product_id, actor):
    """Manager/Super Administrator clears a stuck lock (stale or not) so
    another Operator (or themselves) can proceed — audited either way,
    since this overrides another user's claimed ownership."""
    if actor.role not in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        raise DailyEntryStatusError("Only a Manager or Super Administrator may take over a lock")
    row = get_or_create(date, shift, product_id)
    before = {"lock_user_id": row.lock_user_id, "lock_acquired_at": row.lock_acquired_at.isoformat() if row.lock_acquired_at else None}
    row.lock_user_id = None
    row.lock_acquired_at = None
    db.session.flush()
    record_audit(
        actor, "takeover_lock", "daily_entry_status",
        entity_id=f"{date}|{shift}|{product_id}", before=before, after={"lock_user_id": None},
    )
    return row


def reopen(date, shift, product_id, actor, reason):
    """Manager/Super Administrator reopens a completed entry (real data or
    No Activity Today) so it can be worked on again — always requires a
    reason, always audited, and always leaves the previous completion's
    attribution visible in the audit trail (before/after captures it)."""
    if actor.role not in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        raise DailyEntryStatusError("Only a Manager or Super Administrator may reopen a completed entry")
    if not reason:
        raise DailyEntryStatusError("A reason is required to reopen a completed entry")

    row = get_or_create(date, shift, product_id)
    if not row.completed:
        raise DailyEntryStatusError("This entry has not been completed yet, so there is nothing to reopen")

    before = {
        "completed": row.completed, "completion_type": row.completion_type,
        "completed_by": row.completed_by, "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }
    row.completed = False
    row.completion_type = None
    row.completed_by = None
    row.completed_at = None
    row.lock_user_id = None
    row.lock_acquired_at = None
    row.reopened_by = actor.id
    row.reopened_at = _utcnow()
    row.reopen_reason = reason
    db.session.flush()

    record_audit(
        actor, "reopen", "daily_entry_status",
        entity_id=f"{date}|{shift}|{product_id}", before=before,
        after={"completed": False, "reopened_by": actor.id, "reopen_reason": reason},
    )
    return row


def clear_for_reset(date, shift, product_id, actor, reason):
    """Used only by the Super-Admin 'Reset Daily Values' control (see
    webapp/services/daily_reset_service.py) — clears ownership/completion
    state entirely (not a 'reopen' of a specific completion, a full reset
    of the review state) so the entry starts fresh. Always audited by the
    caller, which already records the broader reset action; this function
    itself makes no separate audit entry to avoid a duplicate log line per
    product in a multi-product reset."""
    row = DailyEntryStatus.query.filter_by(date=date, shift=shift, product_id=product_id).first()
    if row is None:
        return None
    row.lock_user_id = None
    row.lock_acquired_at = None
    row.completed = False
    row.completion_type = None
    row.completed_by = None
    row.completed_at = None
    row.reopened_by = actor.id
    row.reopened_at = _utcnow()
    row.reopen_reason = reason
    db.session.flush()
    return row
