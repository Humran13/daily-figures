"""
Urgent correction — "reset-created zero" investigation.

Round 3 (see test_final_legacy_opening_migration_investigation.py) fixed
the cascading-anchor-demotion bug that silently discarded a legacy row's
own authoritative Opening Stock. This round investigates a DIFFERENT,
newly-reported symptom on the same 8+ products: rows currently classified
`existing_valid_manual_correction` with opening_base_qty=0 turned out NOT
to be genuine Manager corrections at all.

Root cause, proven via reproduction against the REAL
daily_reset_service.execute() and stock_service.upsert_daily_figure():
Reset Daily Values (either mode) correctly clears Opening Stock to a
*non-authoritative* marker (OPENING_STOCK_SOURCE_RESET_CREATED — this
round replaces the previous plain `derived`) — the live view immediately
self-heals, re-deriving the correct historical Opening from whatever real
anchor precedes it. But the reset-cleared row's raw stored value is 0,
and if an elevated user's VERY NEXT save on that exact period resubmits
an explicit Opening of 0 (believing that IS what "Reset" produced, since
that is literally what the stored row now shows), upsert_daily_figure()
correctly-by-its-own-rules-but-wrongly-in-effect treats "0 differs from
live-derived reality" as a deliberate override, permanently locking in
`manual_correction` — silently destroying the ability to ever again trust
the real legacy anchor underneath it.

Fixed via: the reset_created provenance itself (a durable, self-evident
signal); legacy_migration.py's _reset_evidence_for_period() (cross-
references the `reset_daily_values` AuditLog record for this exact
product/date/shift against the DailyFigure's own updated_at); two new
classifications (CLASS_RESET_CREATED_ZERO, CLASS_MISSING_ANCHOR_AFTER_
RESET); and a safe repair extension that RESTORES the quantity (not just
provenance, since this time it was actually overwritten) only for proven
CLASS_MISSING_ANCHOR_AFTER_RESET rows. Also fixed: a later period's own
negative Closing is no longer confirmed CLASS_GENUINE_NEGATIVE until every
EARLIER period for that same product is proven clean — a Night shift's
Closing looking negative purely because the same date's Day shift was
reset (and not yet repaired) is not "genuine."

UPDATE — final reset-safety correction: the save boundary itself
(stock_service.upsert_daily_figure()) was later hardened so an UNWITTING
resave of a reset-left zero can no longer happen at all (it requires an
explicit, authorized, reasoned edit to become manual_correction — see
test_final_reset_safety_explicit_edit_investigation.py). Also, the
default reset mode (figures_only / "Status Only") no longer touches
Opening Stock at all, so it can no longer leave a reset-created zero in
the first place — only Full Reset still clears/recalculates it. The
tests below are updated accordingly: they use Full Reset to reproduce a
cleared period, and an EXPLICIT (reasoned) resave to 0 to represent a
Manager who deliberately — but mistakenly, believing 0 is what the reset
produced — resubmits the reset-left value. That is still a real scenario
the safeguards can't prevent (the system must respect a deliberate
choice), which is exactly why the audit/repair tooling below still
exists and is still exercised here.
"""
import pytest

from webapp.legacy_entries import get_db
from webapp.services import legacy_migration


def _insert_legacy_row(date, shift, product, opening, return_val, production, issued, closing):
    conn = get_db()
    conn.execute("""
        INSERT INTO entries (date, shift, product, opening, return_val, production, issued, closing, notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '2026-07-28T00:00:00')
    """, (date, shift, product, opening, return_val, production, issued, closing))
    conn.commit()
    row_id = conn.execute("SELECT id FROM entries WHERE date=? AND shift=? AND product=?",
                           (date, shift, product)).fetchone()[0]
    conn.close()
    return row_id


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


def _make_product(client, name):
    p = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{p['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})
    return p


def _view(client, pid, date_str, shift="Day"):
    return client.get(f"/api/daily-figures/{pid}?date={date_str}&shift={shift}").get_json()


def _reset(client, date_str, shift, product_id, mode="full", reason="test reset"):
    payload = {"date": date_str, "shift": shift, "product_id": product_id, "mode": mode, "reason": reason}
    if mode == "full":
        payload["confirmation_text"] = f"FULL RESET {date_str} {shift.upper()}"
    return client.post("/api/daily-reset", json=payload)


def _resave_opening_zero(client, product_id, date_str, shift):
    # Final reset-safety correction — a routine (non-explicit) resave can
    # no longer promote a reset-left value into manual_correction at all
    # (see test_final_reset_safety_explicit_edit_investigation.py). This
    # helper now represents a Manager who DELIBERATELY (if mistakenly)
    # resubmits the reset-left 0 via an explicit, reasoned correction —
    # the one remaining real-world way this state can still arise, and
    # exactly what the audit/repair tooling below exists to catch.
    return client.post("/api/daily-figures", json={
        "product_id": product_id, "date": date_str, "shift": shift,
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True,
        "opening_correction_reason": "believed 0 was correct after reset",
    })


def _figure(app, product_id, date_str, shift):
    with app.app_context():
        from webapp.models.daily_figure import DailyFigure
        return DailyFigure.query.filter_by(product_id=product_id, date=date_str, shift=shift).first()


def _repair(app, product_id):
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        preview = legacy_migration.preview_opening_repair(product_id)
        result = legacy_migration.apply_opening_repair(root, product_id, preview["preview_token"])
        _db.session.commit()
        return preview, result


# =====================================================================
# 1-2. Reset produces a non-authoritative marker, not a manual correction
# =====================================================================

def test_reset_sets_opening_to_zero_without_manual_correction_provenance(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    res = _reset(client, "2026-07-20", "Day", p["id"])
    assert res.status_code == 200

    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source == "reset_created"
    assert figure.opening_base_qty == 40000  # recalculated from 2026-07-10's real Closing, never an authoritative zero
    assert figure.opening_stock_source != "manual_correction"


def test_reset_created_zero_is_not_trusted_as_a_permanent_anchor(client, super_admin, app):
    """The live view self-heals immediately: a reset-cleared row is
    non-anchor-eligible, so daily_figure_view() re-derives the true
    Opening from whatever real history precedes it — never showing the
    stored 0 as though it were authoritative."""
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    _reset(client, "2026-07-20", "Day", p["id"])
    view = _view(client, p["id"], "2026-07-20", "Day")
    assert view["opening"]["base_qty"] == 40000  # re-derived from 2026-07-10's real Closing (400.00 Ctns), not 0


# =====================================================================
# 3, 5. Compact Standard restores exactly per the reported arithmetic
# =====================================================================

def test_legacy_opening_can_be_restored_after_a_reset(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    _reset(client, "2026-07-20", "Day", p["id"])
    _resave_opening_zero(client, p["id"], "2026-07-20", "Day")  # the unwitting resave that locks in 0

    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source == "manual_correction"
    assert figure.opening_base_qty == 0

    with app.app_context():
        report = legacy_migration.audit_opening_migration()
    entry = next(e for e in report if e["date"] == "2026-07-20")
    assert entry["classification"] == legacy_migration.CLASS_MISSING_ANCHOR_AFTER_RESET
    assert entry["reset_evidence"] is not None
    assert entry["reset_evidence"]["mode"] == "full"

    _repair(app, p["id"])

    view = _view(client, p["id"], "2026-07-20", "Day")
    assert view["opening"]["base_qty"] == 277760
    assert view["closing"]["base_qty"] == 214410
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["closing"]["cartons"], view["closing"]["packs"], view["closing"]["pieces"], view["packaging_rule"]) == "2144.10 Ctns"

    figure_after = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure_after.opening_stock_source == "legacy_migrated_opening"


# =====================================================================
# 4. Genuine later manual correction remains protected
# =====================================================================

def test_genuine_later_manual_correction_remains_protected_after_reset(client, super_admin, app):
    """A reset happened, but the CURRENT value is a real, deliberate,
    non-zero correction entered well after — never mistaken for a
    reset-created zero, never touched by the repair."""
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    _reset(client, "2026-07-20", "Day", p["id"])
    # A real, deliberate, EXPLICIT correction — a genuine physical count,
    # not the reset's own leftover zero.
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-20", "shift": "Day",
        "opening": {"cartons": 900, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "physical count",
    })

    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source == "manual_correction"
    assert figure.opening_base_qty == 90000

    with app.app_context():
        report = legacy_migration.audit_opening_migration()
        preview = legacy_migration.preview_opening_repair(p["id"])
    entry = next(e for e in report if e["date"] == "2026-07-20")
    assert entry["classification"] == legacy_migration.CLASS_EXISTING_MANUAL_CORRECTION
    assert preview["count"] == 0  # never a repair candidate

    view = _view(client, p["id"], "2026-07-20", "Day")
    assert view["opening"]["base_qty"] == 90000  # the real correction stands, untouched


# =====================================================================
# 6-7. Compact Corporate + Night cascading scenario
# =====================================================================

def test_compact_corporate_restores_exactly(client, super_admin, app):
    p = _make_product(client, "Compact Corporate")
    _insert_legacy_row("2026-07-10", "Day", "Compact Corporate", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Corporate", 180.7, 0, 0, 6.0, 174.7)
    client.post("/api/admin/legacy/migrate")

    _reset(client, "2026-07-20", "Day", p["id"])
    _resave_opening_zero(client, p["id"], "2026-07-20", "Day")

    _repair(app, p["id"])

    view = _view(client, p["id"], "2026-07-20", "Day")
    assert view["opening"]["base_qty"] == 18070
    assert view["closing"]["base_qty"] == 17470
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["closing"]["cartons"], view["closing"]["packs"], view["closing"]["pieces"], view["packaging_rule"]) == "174.70 Ctns"


def test_compact_corporate_night_not_genuine_negative_after_day_anchor_restored(client, super_admin, app):
    """The exact reported scenario: Night's own legacy row (180.70 Opening
    - 6.00 Issued = 174.70 Closing) reconciles fine on its own, but its
    CURRENT Opening reads 0 because the SAME broad reset also cleared
    Night directly. Must never be confirmed genuine_legacy_negative_stock
    — it is repairable, exactly like Day."""
    p = _make_product(client, "Compact Corporate")
    _insert_legacy_row("2026-07-10", "Day", "Compact Corporate", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Corporate", 400.0, 0, 0, 50.0, 350.0)
    _insert_legacy_row("2026-07-20", "Night", "Compact Corporate", 180.7, 0, 0, 6.0, 174.7)
    client.post("/api/admin/legacy/migrate")

    _reset(client, "2026-07-20", "Day", p["id"])
    _reset(client, "2026-07-20", "Night", p["id"])
    _resave_opening_zero(client, p["id"], "2026-07-20", "Day")
    _resave_opening_zero(client, p["id"], "2026-07-20", "Night")

    with app.app_context():
        report = legacy_migration.audit_opening_migration()
    night_entry = next(e for e in report if e["date"] == "2026-07-20" and e["shift"] == "Night")
    assert night_entry["classification"] != legacy_migration.CLASS_GENUINE_NEGATIVE
    assert night_entry["classification"] == legacy_migration.CLASS_MISSING_ANCHOR_AFTER_RESET

    _repair(app, p["id"])

    night_view = _view(client, p["id"], "2026-07-20", "Night")
    assert night_view["opening"]["base_qty"] == 18070
    assert night_view["closing"]["base_qty"] == 17470


def test_later_negative_closing_pending_upstream_repair_is_ambiguous_not_genuine(client, super_admin, app):
    """A later period for the SAME product, whose own notes were cleared
    (so it can't be directly identified as missing-anchor) but whose
    negative Closing derives from an earlier, still-unrepaired period,
    must be classified ambiguous — never confidently "genuine" — until the
    earlier period is fixed."""
    p = _make_product(client, "Compact Corporate")
    _insert_legacy_row("2026-07-10", "Day", "Compact Corporate", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Corporate", 180.7, 0, 0, 6.0, 174.7)
    _insert_legacy_row("2026-07-21", "Day", "Compact Corporate", 174.7, 0, 0, 250.0, -75.3)
    client.post("/api/admin/legacy/migrate")

    _reset(client, "2026-07-20", "Day", p["id"])
    _resave_opening_zero(client, p["id"], "2026-07-20", "Day")
    # The later row is untouched by any reset, but its own notes get
    # cleared by an unrelated later save (e.g. a routine, no-op resave of
    # the SAME already-correct value it holds).
    with app.app_context():
        from webapp.extensions import db as _db
        f = _figure(app, p["id"], "2026-07-21", "Day")
        f.notes = None
        _db.session.commit()

    with app.app_context():
        report = legacy_migration.audit_opening_migration()
    entry_21 = next(e for e in report if e["date"] == "2026-07-21")
    assert entry_21["classification"] == legacy_migration.CLASS_AMBIGUOUS_REVIEW
    assert entry_21["classification"] != legacy_migration.CLASS_GENUINE_NEGATIVE


# =====================================================================
# 8. Status-only reset never changes the true (live-derived) Opening Stock
# =====================================================================

def test_status_only_reset_never_touches_issued_production_returns(client, super_admin, app):
    """Final reset-safety correction, section 2 — MODE_FIGURES_ONLY
    ("Status Only") no longer touches a single DailyFigure column at all,
    Opening Stock included: only workflow/review state is cleared. See
    test_final_reset_safety_explicit_edit_investigation.py for the
    dedicated byte-for-byte-unchanged coverage; this test keeps its
    original Issued/Production/Returns focus."""
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    before = _view(client, p["id"], "2026-07-20", "Day")
    _reset(client, "2026-07-20", "Day", p["id"], mode="figures_only")
    after = _view(client, p["id"], "2026-07-20", "Day")

    assert after["issued"]["base_qty"] == before["issued"]["base_qty"] == 74300
    assert after["return_"]["base_qty"] == before["return_"]["base_qty"] == 10950
    assert after["production"]["base_qty"] == before["production"]["base_qty"] == 0

    # Opening Stock itself is completely untouched by Status Only —
    # still its own correctly-migrated legacy anchor, not re-derived and
    # not zeroed.
    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source == "legacy_migrated_opening"
    assert after["opening"]["base_qty"] == before["opening"]["base_qty"] == 277760


# =====================================================================
# 9. Full Reset remains audited and never erases a historical anchor
# =====================================================================

def test_full_reset_is_audited_and_preserves_earlier_historical_anchor(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    res = client.post("/api/daily-reset", json={
        "date": "2026-07-20", "shift": "Day", "product_id": p["id"], "mode": "full",
        "reason": "test full reset", "confirmation_text": "FULL RESET 2026-07-20 DAY",
    })
    assert res.status_code == 200

    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entries = AuditLog.query.filter_by(action="reset_daily_values").all()
        assert len(entries) == 1
        assert entries[0].username == "root"

    # The earlier period's own historical anchor (2026-07-10) is untouched.
    earlier_view = _view(client, p["id"], "2026-07-10", "Day")
    assert earlier_view["opening"]["base_qty"] == 50000


# =====================================================================
# 10-12. Idempotency, stale token, and audit-log preservation for the
# reset-created-zero repair specifically.
# =====================================================================

def test_rerunning_reset_repair_makes_no_further_change(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _reset(client, "2026-07-20", "Day", p["id"])
    _resave_opening_zero(client, p["id"], "2026-07-20", "Day")

    _repair(app, p["id"])
    with app.app_context():
        preview2 = legacy_migration.preview_opening_repair(p["id"])
    assert preview2["count"] == 0


def test_reset_repair_fails_on_stale_preview_token(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _reset(client, "2026-07-20", "Day", p["id"])
    _resave_opening_zero(client, p["id"], "2026-07-20", "Day")

    with app.app_context():
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        with pytest.raises(legacy_migration.LegacyMigrationRepairConflict):
            legacy_migration.apply_opening_repair(root, p["id"], "stale-or-wrong-token")


def test_reset_repair_writes_its_own_audit_entry_and_preserves_reset_record(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _reset(client, "2026-07-20", "Day", p["id"])
    _resave_opening_zero(client, p["id"], "2026-07-20", "Day")

    with app.app_context():
        from webapp.models.audit_log import AuditLog
        reset_entries_before = AuditLog.query.filter_by(action="reset_daily_values").count()

    _repair(app, p["id"])

    with app.app_context():
        from webapp.models.audit_log import AuditLog
        repair_entries = AuditLog.query.filter_by(action="repair_legacy_opening_migration").all()
        assert len(repair_entries) == 1
        assert repair_entries[0].username == "root"
        # The original reset audit record is never deleted or modified.
        reset_entries_after = AuditLog.query.filter_by(action="reset_daily_values").count()
        assert reset_entries_after == reset_entries_before
