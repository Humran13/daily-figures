"""
Urgent legacy Opening-Stock migration repair investigation.

Root cause, proven empirically against the REAL run_legacy_migration()
(webapp/services/legacy_migration.py), not a hand-rolled reconstruction:
a migrated legacy row's `initial_manual` Opening Stock anchor is *live-
revalidated* on every read (see stock_service._is_trusted_anchor()) — the
moment ANY earlier finalized activity exists, including another legacy
row's OWN migrated Issued StockAdjustment (which every product with more
than one legacy row always has), the row is silently demoted and its
authoritative historical Opening is discarded with no warning. When a
later legacy row's own stated Opening does NOT equal what pure carry-
forward from an earlier anchor would produce (a genuine historical
discontinuity — e.g. a physical stock count), that discontinuity is
silently lost.

Fixed via a new opening_stock_source, `legacy_migrated_opening`
(webapp/models/daily_figure.py), trusted UNCONDITIONALLY like
manual_correction — run_legacy_migration() now writes it for new/re-run
migrations; webapp/services/legacy_migration.py's audit_opening_migration()/
repair tooling handles rows already migrated under the old, vulnerable
initial_manual provenance.
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


def _make_product(client, name, rule=None):
    p = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{p['id']}/packaging-rules", json=rule or {"cartons_to_packs": 10, "packs_to_pieces": 10})
    return p


def _view(client, pid, date_str, shift="Day"):
    return client.get(f"/api/daily-figures/{pid}?date={date_str}&shift={shift}").get_json()


def _force_initial_manual(app, product_id, date_str, shift):
    """Simulates a row created by the OLDER, pre-fix migration (still
    initial_manual) — the exact state a real, already-migrated production
    database would be in before this fix, since run_legacy_migration()
    only ever writes a NEW row once (skips if one already exists)."""
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.daily_figure import DailyFigure
        figure = DailyFigure.query.filter_by(product_id=product_id, date=date_str, shift=shift).first()
        figure.opening_stock_source = "initial_manual"
        _db.session.commit()


# =====================================================================
# COMPACT STANDARD PATTERN — the exact reported live row
# =====================================================================

def test_compact_standard_legacy_values_parse_into_exact_integer_base_units(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    with app.app_context():
        report = legacy_migration.audit_opening_migration()
    entry = next(e for e in report if e["entries_id"] == 1)
    assert entry["legacy_base_units"]["opening"] == 277760
    assert entry["legacy_base_units"]["return_val"] == 10950
    assert entry["legacy_base_units"]["issued"] == 74300
    assert entry["legacy_base_units"]["closing"] == 214410


def test_compact_standard_legacy_equation_reconciles_exactly(client, super_admin, app):
    _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    with app.app_context():
        report = legacy_migration.audit_opening_migration()
    entry = report[0]
    assert entry["legacy_equation_reconciles"] is True
    assert entry["legacy_recalculated_closing_base_qty"] == 214410


def test_compact_standard_missing_opening_produces_old_negative_result(client, super_admin, app):
    """Reproduces the OLD (pre-fix) buggy behavior directly: a legacy row
    still classified initial_manual, with ANOTHER earlier legacy row's
    StockAdjustment predating it, has its authoritative Opening silently
    discarded."""
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 0.0, 0, 0, 100.0, -100.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _force_initial_manual(app, p["id"], "2026-07-20", "Day")

    view = _view(client, p["id"], "2026-07-20", "Day")
    assert view["opening"]["base_qty"] != 277760  # the authoritative legacy opening is being ignored


def test_compact_standard_repair_restores_correct_ctns(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 0.0, 0, 0, 100.0, -100.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _force_initial_manual(app, p["id"], "2026-07-20", "Day")

    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        preview = legacy_migration.preview_opening_repair(p["id"])
        candidate = next(c for c in preview["candidates"] if c["date"] == "2026-07-20")
        assert candidate is not None
        root = User.query.filter_by(username="root").first()
        result = legacy_migration.apply_opening_repair(root, p["id"], preview["preview_token"])
        _db.session.commit()
        assert result["count"] >= 1

    view = _view(client, p["id"], "2026-07-20", "Day")
    assert view["opening"]["base_qty"] == 277760
    assert view["closing"]["base_qty"] == 214410
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["closing"]["cartons"], view["closing"]["packs"], view["closing"]["pieces"], view["packaging_rule"]) == "2144.10 Ctns"


def test_compact_standard_following_period_opening_matches_repaired_closing(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 0.0, 0, 0, 100.0, -100.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _force_initial_manual(app, p["id"], "2026-07-20", "Day")

    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        preview = legacy_migration.preview_opening_repair(p["id"])
        root = User.query.filter_by(username="root").first()
        legacy_migration.apply_opening_repair(root, p["id"], preview["preview_token"])
        _db.session.commit()

    assert _view(client, p["id"], "2026-07-21", "Day")["opening"]["base_qty"] == 214410


def test_compact_standard_migrated_issued_adjustment_remains_intact_after_repair(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _force_initial_manual(app, p["id"], "2026-07-20", "Day")

    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        from webapp.models.daily_figure import StockAdjustment
        before_count = StockAdjustment.query.filter_by(product_id=p["id"]).count()
        preview = legacy_migration.preview_opening_repair(p["id"])
        root = User.query.filter_by(username="root").first()
        legacy_migration.apply_opening_repair(root, p["id"], preview["preview_token"])
        _db.session.commit()
        after_count = StockAdjustment.query.filter_by(product_id=p["id"]).count()
        assert after_count == before_count == 1


def test_compact_standard_no_double_counting_after_repair(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _force_initial_manual(app, p["id"], "2026-07-20", "Day")

    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        preview = legacy_migration.preview_opening_repair(p["id"])
        root = User.query.filter_by(username="root").first()
        legacy_migration.apply_opening_repair(root, p["id"], preview["preview_token"])
        _db.session.commit()

    # Closing must still be exactly the reconciled legacy value — not the
    # legacy opening PLUS a duplicated issued/returns contribution.
    assert _view(client, p["id"], "2026-07-20", "Day")["closing"]["base_qty"] == 214410


# =====================================================================
# COMPACT CORPORATE PATTERN
# =====================================================================

def test_compact_corporate_missing_opening_produces_old_negative_result(client, super_admin, app):
    p = _make_product(client, "Compact Corporate")
    _insert_legacy_row("2026-07-10", "Day", "Compact Corporate", 0.0, 0, 0, 50.0, -50.0)
    _insert_legacy_row("2026-07-20", "Night", "Compact Corporate", 180.7, 0, 0, 6.0, 174.7)
    client.post("/api/admin/legacy/migrate")
    _force_initial_manual(app, p["id"], "2026-07-20", "Night")

    view = _view(client, p["id"], "2026-07-20", "Night")
    assert view["opening"]["base_qty"] != 18070


def test_compact_corporate_repair_restores_correct_ctns(client, super_admin, app):
    p = _make_product(client, "Compact Corporate")
    _insert_legacy_row("2026-07-10", "Day", "Compact Corporate", 0.0, 0, 0, 50.0, -50.0)
    _insert_legacy_row("2026-07-20", "Night", "Compact Corporate", 180.7, 0, 0, 6.0, 174.7)
    client.post("/api/admin/legacy/migrate")
    _force_initial_manual(app, p["id"], "2026-07-20", "Night")

    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        preview = legacy_migration.preview_opening_repair(p["id"])
        root = User.query.filter_by(username="root").first()
        legacy_migration.apply_opening_repair(root, p["id"], preview["preview_token"])
        _db.session.commit()

    view = _view(client, p["id"], "2026-07-20", "Night")
    assert view["opening"]["base_qty"] == 18070
    assert view["closing"]["base_qty"] == 17470
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["closing"]["cartons"], view["closing"]["packs"], view["closing"]["pieces"], view["packaging_rule"]) == "174.70 Ctns"


def test_compact_corporate_later_row_with_production_and_returns_reconciles(client, super_admin, app):
    """The later row: Opening 174.7, Production 1.0, Returns 1.0, Issued
    1.0, Closing 175.7 — proves only the correct anchor or discontinuity
    is created, and later carry-forward stays consistent."""
    p = _make_product(client, "Compact Corporate")
    _insert_legacy_row("2026-07-20", "Night", "Compact Corporate", 180.7, 0, 0, 6.0, 174.7)
    _insert_legacy_row("2026-07-21", "Day", "Compact Corporate", 174.7, 1.0, 1.0, 1.0, 175.7)
    client.post("/api/admin/legacy/migrate")

    with app.app_context():
        report = legacy_migration.audit_opening_migration()
    entry_21 = next(e for e in report if e["date"] == "2026-07-21")
    assert entry_21["legacy_equation_reconciles"] is True
    # No discontinuity here (174.7 carries straight in) — the second row
    # does not strictly need its own separate anchor, but since the
    # migration always writes one per legacy row, it is still correctly
    # legacy_migrated_opening and reconciles on its own terms too.
    assert entry_21["classification"] == legacy_migration.CLASS_ALREADY_MIGRATED_CORRECTLY

    view = _view(client, p["id"], "2026-07-21", "Day")
    assert view["closing"]["base_qty"] == 17570


# =====================================================================
# OTHER PATTERNS
# =====================================================================

def test_zero_opening_damage_product_remains_unchanged(client, super_admin, app):
    p = _make_product(client, "Compact Damage")
    _insert_legacy_row("2026-07-20", "Day", "Compact Damage", 0.0, 0, 0, 0, 0.0)
    client.post("/api/admin/legacy/migrate")

    with app.app_context():
        report = legacy_migration.audit_opening_migration()
    entry = report[0]
    assert entry["classification"] == legacy_migration.CLASS_ALREADY_MIGRATED_CORRECTLY
    assert _view(client, p["id"], "2026-07-20", "Day")["opening"]["base_qty"] == 0


def test_genuine_legacy_negative_closing_remains_negative(client, super_admin, app):
    """Old book notation can never record a negative raw value at all
    (decode_legacy_value rejects it outright) — so a real over-issuance
    against a correctly-migrated opening shows up only in the CURRENT
    computed closing, not in the legacy row's own decoded fields. The
    legacy row here (Opening 10.0, Issued 6.0, Closing 4.0) fully
    reconciles and migrates cleanly with a non-negative closing — but a
    real LATER adjustment (e.g. a damage/loss entry recorded through the
    modern system, on the same period) pushes the live ledger genuinely
    negative. That is real recorded business activity, not a migration
    defect, and must never be clamped to zero."""
    p = _make_product(client, "Napkins Corporate")
    _insert_legacy_row("2026-07-20", "Day", "Napkins Corporate", 10.0, 0, 0, 6.0, 4.0)
    client.post("/api/admin/legacy/migrate")
    client.post("/api/daily-figures/adjustments", json={
        "product_id": p["id"], "date": "2026-07-20", "shift": "Day", "delta_base_qty": 1000, "reason": "damage",
    })

    with app.app_context():
        report = legacy_migration.audit_opening_migration()
    entry = report[0]
    assert entry["classification"] == legacy_migration.CLASS_GENUINE_NEGATIVE
    assert _view(client, p["id"], "2026-07-20", "Day")["closing"]["base_qty"] == -600  # never forced to zero or positive


def test_mismatched_legacy_equation_is_flagged_and_not_auto_repaired(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 100.0, 0, 0, 0, 999.0)
    client.post("/api/admin/legacy/migrate")

    with app.app_context():
        report = legacy_migration.audit_opening_migration()
        preview = legacy_migration.preview_opening_repair(p["id"])
    entry = report[0]
    assert entry["classification"] == legacy_migration.CLASS_DOES_NOT_RECONCILE
    assert preview["count"] == 0  # never a repair candidate


def test_real_later_manual_correction_is_preserved(client, super_admin, app):
    # An earlier legacy row is required so the target row is NOT this
    # product's first-ever period — otherwise any later submission is
    # itself just the initial anchor (opening_stock_source=initial_manual),
    # not a true manual_correction (which only exists when it overrides an
    # already-derivable running balance).
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-10", "Day", "Compact Standard", 0.0, 0, 0, 0.0, 0.0)
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _force_initial_manual(app, p["id"], "2026-07-20", "Day")

    # A real Manager correction supersedes the migrated row entirely.
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-20", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
    })

    with app.app_context():
        report = legacy_migration.audit_opening_migration()
        preview = legacy_migration.preview_opening_repair(p["id"])
    entry = next(e for e in report if e["date"] == "2026-07-20")
    assert entry["classification"] == legacy_migration.CLASS_EXISTING_MANUAL_CORRECTION
    assert preview["count"] == 0  # never repaired

    assert _view(client, p["id"], "2026-07-20", "Day")["opening"]["base_qty"] == 50000  # the real correction stands


def test_rerunning_repair_changes_nothing(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _force_initial_manual(app, p["id"], "2026-07-20", "Day")

    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        preview1 = legacy_migration.preview_opening_repair(p["id"])
        legacy_migration.apply_opening_repair(root, p["id"], preview1["preview_token"])
        _db.session.commit()

        preview2 = legacy_migration.preview_opening_repair(p["id"])
        assert preview2["count"] == 0  # nothing left to repair — idempotent


def test_repair_fails_if_preview_token_is_stale(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _force_initial_manual(app, p["id"], "2026-07-20", "Day")

    with app.app_context():
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        with pytest.raises(legacy_migration.LegacyMigrationRepairConflict):
            legacy_migration.apply_opening_repair(root, p["id"], "stale-or-wrong-token")


def test_every_audit_action_is_preserved_in_audit_log(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")
    _force_initial_manual(app, p["id"], "2026-07-20", "Day")

    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        from webapp.models.audit_log import AuditLog
        root = User.query.filter_by(username="root").first()
        preview = legacy_migration.preview_opening_repair(p["id"])
        legacy_migration.apply_opening_repair(root, p["id"], preview["preview_token"])
        _db.session.commit()

        entries = AuditLog.query.filter_by(action="repair_legacy_opening_migration").all()
        assert len(entries) == 1
        assert entries[0].username == "root"


# =====================================================================
# NEGATIVE CARTON FORMATTING
# =====================================================================

def test_negative_600_base_units_displays_as_signed_ctns():
    from webapp.services.quantity_format import qty_label
    rule = {"carton_to_pieces": None, "cartons_to_packs": 10, "packs_to_pieces": 10}
    assert qty_label(-6, 0, 0, rule) == "-6.00 Ctns"


def test_negative_63350_base_units_displays_as_signed_ctns():
    from webapp.services.quantity_format import qty_label
    rule = {"carton_to_pieces": None, "cartons_to_packs": 10, "packs_to_pieces": 10}
    assert qty_label(-633, 5, 0, rule) == "-633.50 Ctns"


def test_positive_formatting_remains_unchanged():
    from webapp.services.quantity_format import qty_label
    rule = {"carton_to_pieces": None, "cartons_to_packs": 10, "packs_to_pieces": 10}
    assert qty_label(6, 0, 0, rule) == "6 Ctns"
    assert qty_label(235, 0, 0, rule) == "235 Ctns"


def test_zero_formatting_remains_unchanged():
    from webapp.services.quantity_format import qty_label
    rule = {"carton_to_pieces": None, "cartons_to_packs": 10, "packs_to_pieces": 10}
    assert qty_label(0, 0, 0, rule) == "0 Ctns"


def test_sub_carton_negative_magnitude_preserves_sign(client, super_admin, app):
    """A negative value smaller than one whole carton splits to
    cartons=0, and `-0 == 0` for an int — the sign must fall onto `packs`
    (or `pieces`, if packs is also 0) instead, never silently vanish."""
    p = _make_product(client, "Sub Carton Sign Product")
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.product import Product
        from webapp.services.quantity_format import qty_label
        from webapp.services.stock_service import _split_or_none
        product = _db.session.get(Product, p["id"])
        rule = product.current_packaging_rule()

        split = _split_or_none(-50, rule)
        assert split == {"cartons": 0, "packs": -5, "pieces": 0}
        assert qty_label(split["cartons"], split["packs"], split["pieces"], rule) == "-0.50 Ctns"

        split_pieces_only = _split_or_none(-3, rule)
        assert split_pieces_only == {"cartons": 0, "packs": 0, "pieces": -3}
        assert qty_label(split_pieces_only["cartons"], split_pieces_only["packs"], split_pieces_only["pieces"], rule) == "-0.03 Ctns"


def test_no_pack_tier_negative_formatting_correct():
    from webapp.services.quantity_format import qty_label
    rule = {"carton_to_pieces": 60}
    assert qty_label(-5, 0, 3, rule) == "-5.03 Ctns"
    assert qty_label(5, 0, 3, rule) == "5.03 Ctns"


def test_no_raw_base_unit_number_receives_a_ctns_suffix(client, super_admin):
    p = _make_product(client, "Negative Format Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures/adjustments", json={
        "product_id": p["id"], "date": "2026-07-19", "shift": "Day", "delta_base_qty": 10000, "reason": "test",
    })
    view = _view(client, p["id"], "2026-07-19")
    assert view["closing"]["base_qty"] == -9500
    assert "warning" not in view["closing"]
    from webapp.services.quantity_format import qty_label
    label = qty_label(view["closing"]["cartons"], view["closing"]["packs"], view["closing"]["pieces"], view["packaging_rule"])
    assert label == "-95.00 Ctns"
    assert "-9500" not in label


# =====================================================================
# Reset preview legacy visibility
# =====================================================================

def test_reset_preview_shows_legacy_opening_and_production_returns_components(client, super_admin, app):
    p = _make_product(client, "Compact Standard")
    _insert_legacy_row("2026-07-20", "Day", "Compact Standard", 2777.6, 109.5, 0, 743.0, 2144.1)
    client.post("/api/admin/legacy/migrate")

    preview = client.post("/api/daily-reset/preview", json={
        "date": "2026-07-20", "shift": "Day", "product_id": p["id"], "mode": "figures_only",
    }).get_json()
    row = preview["products"][0]
    assert row["is_legacy_migrated_opening"] is True
    assert row["legacy_returns_component"] == 10950
    assert row["opening_stock_source"] == "legacy_migrated_opening"
