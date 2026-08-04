"""
Final reset safety fix — prevent a stale zero from becoming an
authoritative manual Opening Stock correction.

The prior "reset-created zero" investigation identified the root cause
(a reset-cleared row's stale, stored 0 could be resubmitted by a later
save and locked in forever as manual_correction) and built diagnostic
tooling, but did not change the save boundary itself — so the same
corruption could still recur. This round fixes it at the source:

1. Opening Stock only ever becomes manual_correction when the caller
   explicitly says so (`opening_stock_explicitly_edited=True`) AND is
   actually authorized for it — validated server-side in
   stock_service.upsert_daily_figure(), never trusted from the client
   alone. Merely differing from live-derived reality is no longer
   sufficient by itself (the old heuristic that caused the bug).
2. A routine (non-explicit) save NEVER disturbs an already-authoritative
   row (manual_correction or legacy_migrated_opening) — preserved
   byte-for-byte, ignoring whatever was submitted, including a stale
   zero, an outdated positive number, or an outdated negative number
   left over from an old browser tab.
3. MODE_FIGURES_ONLY ("Reset Daily Figures Status Only") no longer
   touches a single Opening Stock column (or Production/Returns/Issued,
   or any source-book record) at all — only workflow/review state.
4. MODE_FULL ("Full Reset") still legitimately clears Opening Stock for
   its own target period, but never to an authoritative zero — it is
   recalculated from the real previous chronological Closing Stock and
   stored as the non-authoritative `reset_created` marker.
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
    conn.close()


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


def _make_product(client, name):
    p = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{p['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})
    return p


def _view(client, pid, date_str, shift="Day"):
    return client.get(f"/api/daily-figures/{pid}?date={date_str}&shift={shift}").get_json()


def _figure(app, product_id, date_str, shift):
    with app.app_context():
        from webapp.models.daily_figure import DailyFigure
        return DailyFigure.query.filter_by(product_id=product_id, date=date_str, shift=shift).first()


def _reset(client, date_str, shift, product_id, mode="figures_only", reason="test reset"):
    payload = {"date": date_str, "shift": shift, "product_id": product_id, "mode": mode, "reason": reason}
    if mode == "full":
        payload["confirmation_text"] = f"FULL RESET {date_str} {shift.upper()}"
    return client.post("/api/daily-reset", json=payload)


def _explicit_save(client, product_id, date_str, shift, cartons, reason="physical count"):
    return client.post("/api/daily-figures", json={
        "product_id": product_id, "date": date_str, "shift": shift,
        "opening": {"cartons": cartons, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": reason,
    })


def _routine_save(client, product_id, date_str, shift, cartons):
    return client.post("/api/daily-figures", json={
        "product_id": product_id, "date": date_str, "shift": shift,
        "opening": {"cartons": cartons, "packs": 0, "pieces": 0},
    })


# =====================================================================
# STATUS ONLY
# =====================================================================

def test_status_only_does_not_change_opening_quantity_or_provenance(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    before = _figure(app, p["id"], "2026-07-20", "Day")
    before_snapshot = (before.opening_cartons, before.opening_packs, before.opening_pieces,
                        before.opening_base_qty, before.opening_stock_source,
                        before.opening_stock_is_override, before.notes, before.updated_at)

    res = _reset(client, "2026-07-20", "Day", p["id"], mode="figures_only")
    assert res.status_code == 200

    after = _figure(app, p["id"], "2026-07-20", "Day")
    after_snapshot = (after.opening_cartons, after.opening_packs, after.opening_pieces,
                       after.opening_base_qty, after.opening_stock_source,
                       after.opening_stock_is_override, after.notes, after.updated_at)
    assert before_snapshot == after_snapshot


def test_status_only_does_not_change_production_returns_issued(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    before = _view(client, p["id"], "2026-07-20", "Day")
    _reset(client, "2026-07-20", "Day", p["id"], mode="figures_only")
    after = _view(client, p["id"], "2026-07-20", "Day")

    assert after["production"]["base_qty"] == before["production"]["base_qty"]
    assert after["return_"]["base_qty"] == before["return_"]["base_qty"] == 10950
    assert after["issued"]["base_qty"] == before["issued"]["base_qty"] == 74300
    assert after["closing"]["base_qty"] == before["closing"]["base_qty"]


def test_status_only_does_not_alter_source_records(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    with app.app_context():
        from webapp.models.daily_figure import StockAdjustment
        before_count = StockAdjustment.query.filter_by(product_id=p["id"]).count()

    _reset(client, "2026-07-20", "Day", p["id"], mode="figures_only")

    with app.app_context():
        from webapp.models.daily_figure import StockAdjustment
        after_count = StockAdjustment.query.filter_by(product_id=p["id"]).count()
    assert after_count == before_count


def test_status_only_clears_only_workflow_review_state(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-20", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-entry-status/no-activity", json={
        "product_id": p["id"], "date": "2026-07-21", "shift": "Day",
    })
    status_before = client.get(f"/api/daily-entry-status?date=2026-07-21&shift=Day&product_id={p['id']}").get_json()
    assert status_before["status"] == "completed"

    _reset(client, "2026-07-21", "Day", p["id"], mode="figures_only")

    status_after = client.get(f"/api/daily-entry-status?date=2026-07-21&shift=Day&product_id={p['id']}").get_json()
    assert status_after["status"] == "not_started"


# =====================================================================
# FULL RESET
# =====================================================================

def test_full_reset_does_not_create_an_authoritative_zero(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    _reset(client, "2026-07-20", "Day", p["id"], mode="full")

    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source == "reset_created"
    assert figure.opening_base_qty != 0
    assert figure.opening_base_qty == 40000  # recalculated from 2026-07-10's real Closing


def test_full_reset_following_read_derives_opening_from_prior_closing(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    _reset(client, "2026-07-20", "Day", p["id"], mode="full")
    view = _view(client, p["id"], "2026-07-20", "Day")
    assert view["opening"]["base_qty"] == 40000


def test_full_reset_row_remains_reset_created_provenance(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-20", "shift": "Day",
        "opening": {"cartons": 900, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "count",
    })
    _reset(client, "2026-07-20", "Day", p["id"], mode="full")
    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source == "reset_created"


def test_full_reset_routine_save_preserves_the_derived_opening(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _reset(client, "2026-07-20", "Day", p["id"], mode="full")

    _routine_save(client, p["id"], "2026-07-20", "Day", 0)  # stale/no-op resave, no explicit flag

    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source != "manual_correction"
    assert figure.opening_base_qty == 40000


# =====================================================================
# STALE SAVE
# =====================================================================

def test_stale_zero_after_reset_without_explicit_intent_is_ignored(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _reset(client, "2026-07-20", "Day", p["id"], mode="full")

    res = _routine_save(client, p["id"], "2026-07-20", "Day", 0)
    assert res.status_code == 200
    assert res.get_json()["opening"]["base_qty"] == 40000  # never the stale 0

    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source != "manual_correction"


def test_stale_outdated_positive_value_without_explicit_intent_is_ignored(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _reset(client, "2026-07-20", "Day", p["id"], mode="full")

    res = _routine_save(client, p["id"], "2026-07-20", "Day", 999)  # an old tab's stale different value
    assert res.status_code == 200
    assert res.get_json()["opening"]["base_qty"] == 40000  # ignored, still the true derived value

    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source != "manual_correction"
    assert figure.opening_base_qty == 40000


def test_optimistic_behavior_remains_correct_after_stale_save_rejected(client, super_admin, app):
    """A stale, ignored save must not corrupt the row for a SUBSEQUENT
    genuine, correctly-derived read/save cycle — the row keeps working
    normally afterward."""
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _reset(client, "2026-07-20", "Day", p["id"], mode="full")
    _routine_save(client, p["id"], "2026-07-20", "Day", 0)  # stale save, ignored

    # A genuine later explicit correction still works normally.
    res = _explicit_save(client, p["id"], "2026-07-20", "Day", 900)
    assert res.status_code == 200
    assert res.get_json()["opening"]["base_qty"] == 90000
    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source == "manual_correction"


# =====================================================================
# EXPLICIT EDIT
# =====================================================================

def test_authorized_manager_explicit_edit_creates_manual_correction(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    client.post("/api/admin/legacy/migrate")

    res = _explicit_save(client, p["id"], "2026-07-20", "Day", 900, reason="physical count")
    assert res.status_code == 200
    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source == "manual_correction"
    assert figure.opening_base_qty == 90000


def test_explicit_correction_reason_and_actor_are_audited(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    client.post("/api/admin/legacy/migrate")

    _explicit_save(client, p["id"], "2026-07-20", "Day", 900, reason="physical stock count")

    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="correct_opening_stock").order_by(AuditLog.id.desc()).first()
    assert entry is not None
    assert entry.username == "root"
    assert "physical stock count" in (entry.after_json or "")


def test_explicit_correction_to_zero_is_allowed(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    client.post("/api/admin/legacy/migrate")

    res = _explicit_save(client, p["id"], "2026-07-20", "Day", 0, reason="shelf verified empty")
    assert res.status_code == 200
    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source == "manual_correction"
    assert figure.opening_base_qty == 0


def test_viewer_cannot_forge_explicit_edit_flag(client, login_as, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    client.post("/api/admin/legacy/migrate")
    login_as("viewer_test", "password123", "viewer")

    res = client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-20", "shift": "Day",
        "opening": {"cartons": 900, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "forged",
    })
    assert res.status_code == 403  # Viewer forbidden outright by the route
    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure is None or figure.opening_stock_source != "manual_correction"


def test_unauthorized_operator_cannot_forge_explicit_edit_flag(client, login_as, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    client.post("/api/admin/legacy/migrate")
    login_as("op_forge_test", "password123", "operator")

    res = client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-20", "shift": "Day",
        "opening": {"cartons": 900, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "forged",
    })
    assert res.status_code == 200  # not forbidden outright, but silently NOT honored
    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source != "manual_correction"


def test_authorized_operator_can_explicitly_edit_opening(client, login_as, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    client.post("/api/admin/legacy/migrate")
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_authorized_test", "password123", "operator")

    res = client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-20", "shift": "Day",
        "opening": {"cartons": 900, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "authorized correction",
    })
    assert res.status_code == 200
    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source == "manual_correction"


def test_missing_reason_is_rejected(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 500.0, 0, 0, 100.0, 400.0)
    client.post("/api/admin/legacy/migrate")

    res = client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-20", "shift": "Day",
        "opening": {"cartons": 900, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True,
    })
    assert res.status_code == 400
    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure is None or figure.opening_stock_source != "manual_correction"


# =====================================================================
# LEGACY PATTERNS
# =====================================================================

def test_compact_standard_repaired_anchor_survives_reset_and_routine_save(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 0.0, 0, 0, 100.0, -100.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    with app.app_context():
        from webapp.extensions import db as _db
        f = _figure(app, p["id"], "2026-07-20", "Day")
        f.opening_stock_source = "initial_manual"
        _db.session.commit()

    with app.app_context():
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        preview = legacy_migration.preview_opening_repair(p["id"])
        legacy_migration.apply_opening_repair(root, p["id"], preview["preview_token"])
        _db.session.commit()

    view = _view(client, p["id"], "2026-07-20", "Day")
    assert view["opening"]["base_qty"] == 277760
    assert view["closing"]["base_qty"] == 214410
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["closing"]["cartons"], view["closing"]["packs"], view["closing"]["pieces"], view["packaging_rule"]) == "2144.10 Ctns"

    # A later Status Only reset on an UNRELATED period does not affect it.
    _reset(client, "2026-07-10", "Day", p["id"], mode="figures_only")
    still = _view(client, p["id"], "2026-07-20", "Day")
    assert still["opening"]["base_qty"] == 277760

    # A routine (non-explicit) resave never overwrites it.
    _routine_save(client, p["id"], "2026-07-20", "Day", 0)
    figure = _figure(app, p["id"], "2026-07-20", "Day")
    assert figure.opening_stock_source == "legacy_migrated_opening"
    assert figure.opening_base_qty == 277760


def test_compact_corporate_repaired_anchor_remains_174_70(client, super_admin, app):
    p = _make_product(client, "Compact Corporate")
    _insert_legacy_row("2026-07-10", "Day", "Compact Corporate", 0.0, 0, 0, 50.0, -50.0)
    _insert_legacy_row("2026-07-20", "Night", "Compact Corporate", 180.7, 0, 0, 6.0, 174.7)
    client.post("/api/admin/legacy/migrate")
    with app.app_context():
        from webapp.extensions import db as _db
        f = _figure(app, p["id"], "2026-07-20", "Night")
        f.opening_stock_source = "initial_manual"
        _db.session.commit()

    with app.app_context():
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        preview = legacy_migration.preview_opening_repair(p["id"])
        legacy_migration.apply_opening_repair(root, p["id"], preview["preview_token"])
        _db.session.commit()

    view = _view(client, p["id"], "2026-07-20", "Night")
    assert view["opening"]["base_qty"] == 18070
    assert view["closing"]["base_qty"] == 17470
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["closing"]["cartons"], view["closing"]["packs"], view["closing"]["pieces"], view["packaging_rule"]) == "174.70 Ctns"

    _routine_save(client, p["id"], "2026-07-20", "Night", 0)
    figure = _figure(app, p["id"], "2026-07-20", "Night")
    assert figure.opening_stock_source == "legacy_migrated_opening"
    assert figure.opening_base_qty == 18070


def test_following_period_opening_equals_prior_closing(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    following = _view(client, p["id"], "2026-07-21", "Day")
    assert following["opening"]["base_qty"] == 214410
