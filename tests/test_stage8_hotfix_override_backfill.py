"""
Stage 8 hotfix — the e91b6f3a2c07 migration's backfill rule ("each
product's chronologically earliest DailyFigure row is a genuine Opening
Stock anchor") was proven wrong by a staging diagnostic: Compact Standard
had finalized Production on 19 July 2026 with no DailyFigure row at all,
then a later zero row on 1 August 2026 that the backfill marked an
override purely for being earliest — permanently freezing 1 August's
Opening Stock at 0 instead of deriving 109.50 Ctns (10,950 base units).

Two independent fixes, both exercised here:

1. A corrective migration (d44646dd7efe) that repairs already-mismarked
   rows: for each product's single earliest DailyFigure row, if any
   finalized Production/Returns/Dispatch/Adjustment predates it, its
   override flag is cleared.
2. A runtime fix in webapp/services/stock_service.py's
   get_prior_closing_base_qty(): when no override anchor exists at all,
   the balance is now derived from zero using every finalized movement
   before the target period (instead of returning None and letting
   upsert_daily_figure() treat any first DailyFigure row as an automatic
   anchor) — so the SAME bug can never recur for a product that hasn't
   had its first DailyFigure row created yet.

Everything here operates by product_id and each product's own packaging
rule — nothing is hard-coded to Compact Standard or any specific product
name, per the hotfix's explicit universal-scope requirement.
"""
import pytest

from webapp.extensions import db as _db
from webapp.models.daily_figure import OPENING_STOCK_SOURCE_LEGACY_INFERRED, DailyFigure
from migrations.versions import d44646dd7efe_repair_opening_stock_override_backfill as repair_migration


def _run_repair_migration(app):
    """Applies the corrective migration's exact repair logic against the
    current test database connection — the same SQL the real Alembic
    migration runs, without needing a separate subprocess-driven Alembic
    invocation (this project's migration round-trips are verified
    manually via `flask db upgrade/downgrade`, not through pytest — see
    DEPLOY.md)."""
    with app.app_context():
        bind = _db.session.connection()
        product_ids = [row[0] for row in bind.execute(_db.text(
            "SELECT DISTINCT product_id FROM daily_figures"
        ))]
        for product_id in product_ids:
            earliest = bind.execute(_db.text(
                "SELECT id, date, shift FROM daily_figures WHERE product_id = :pid "
                "ORDER BY date ASC, (CASE WHEN shift='Day' THEN 0 ELSE 1 END) ASC, id ASC LIMIT 1"
            ), {"pid": product_id}).first()
            if earliest is None:
                continue
            row_id, row_date, row_shift = earliest
            currently_override = bind.execute(_db.text(
                "SELECT opening_stock_is_override FROM daily_figures WHERE id = :id"
            ), {"id": row_id}).scalar()
            if not currently_override:
                continue
            row_shift_order = 0 if row_shift == "Day" else 1
            if repair_migration._any_finalized_movement_before(bind, product_id, row_date, row_shift_order):
                bind.execute(
                    _db.text("UPDATE daily_figures SET opening_stock_is_override = 0 WHERE id = :id"),
                    {"id": row_id},
                )
        _db.session.commit()


def _make_product(client, name, cartons_to_packs=10, packs_to_pieces=10, carton_to_pieces=None):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    payload = {"cartons_to_packs": cartons_to_packs, "packs_to_pieces": packs_to_pieces} \
        if carton_to_pieces is None else {"carton_to_pieces": carton_to_pieces}
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json=payload)
    return product


def _finalize_production(client, product_id, date_str, shift, cartons, packs=0, pieces=0):
    p = client.post("/api/production", json={
        "date": date_str, "shift": shift,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    res = client.post(f"/api/production/{p['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return p


def _finalize_return(client, product_id, date_str, cartons, packs=0, pieces=0):
    r = client.post("/api/returns", json={
        "date": date_str,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    res = client.post(f"/api/returns/{r['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return r


def _finalize_dispatch(client, product_id, customer_id, date_str, shift, cartons, number, packs=0, pieces=0):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date_str, "shift": shift, "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return d


def _view(client, product_id, date_str, shift="Day"):
    return client.get(f"/api/daily-figures/{product_id}?date={date_str}&shift={shift}").get_json()


def _insert_stale_override_row(app, product_id, rule_id, date, shift, user_id,
                                source=OPENING_STOCK_SOURCE_LEGACY_INFERRED):
    """Simulates a row exactly as it would exist post-e91b6f3a2c07/
    d44646dd7efe (the previous hotfix round) — opening_stock_is_override=
    True with no informed opening_stock_source classification (the new
    column this round introduces defaults new/unclassified rows to
    legacy_inferred; see the new provenance migration). Anchor-eligible
    but — like initial_manual — always live-revalidated against finalized
    movement (see stock_service._is_trusted_anchor()), which is precisely
    what now catches this exact stale-row scenario at READ time, without
    needing any repair migration to run first."""
    with app.app_context():
        row = DailyFigure(
            product_id=product_id, date=date, shift=shift,
            opening_cartons=0, opening_packs=0, opening_pieces=0, opening_base_qty=0,
            opening_stock_is_override=True,
            opening_stock_source=source,
            return_cartons=0, return_packs=0, return_pieces=0, return_base_qty=0,
            production_cartons=0, production_packs=0, production_pieces=0, production_base_qty=0,
            packaging_rule_id=rule_id, created_by=user_id, updated_by=user_id,
        )
        _db.session.add(row)
        _db.session.commit()
        return row.id


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = _make_product(client, "Compact Standard Hotfix")
    category = client.post("/api/admin/sales-categories", json={"name": "Hotfix Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Hotfix Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "customer": customer}


# =====================================================================
# The exact staging reproduction, repaired via runtime endpoint
# =====================================================================

def test_exact_staging_repro_repaired_through_the_real_endpoint(client, login_as, app):
    """A later production hotfix (see test_stage8_production_hotfix.py)
    replaced the plain opening_stock_is_override boolean with an explicit
    opening_stock_source, and made anchor trust for every non-
    manual_correction source (including legacy_inferred, what this stale
    row is now classified as) a LIVE check against finalized history —
    re-evaluated on every read, not decided once by a migration. That
    means this exact scenario is now caught at read time, before the old
    d44646dd7efe repair migration even runs; running it afterward is a
    no-op (there's nothing left for it to fix) and is asserted here purely
    to prove it stays safe to run against already-correct data."""
    setup = _setup_locals(client, login_as)
    pid = setup["product"]["id"]
    rule_id = setup["rule_id"]

    _finalize_production(client, pid, "2026-07-19", "Day", 109, 5, 0)
    assert DailyFigure.query.filter_by(product_id=pid).count() == 0  # no row created

    row_id = _insert_stale_override_row(app, pid, rule_id, "2026-08-01", "Day", setup["user_id"])
    with app.app_context():
        assert _db.session.get(DailyFigure, row_id).opening_stock_is_override is True

    already_correct = _view(client, pid, "2026-08-01")
    assert already_correct["opening"]["base_qty"] == 10950  # fixed live, no migration needed

    _run_repair_migration(app)  # safe no-op against already-correct data

    after = _view(client, pid, "2026-08-01")
    assert after["opening"]["base_qty"] == 10950
    assert after["opening"]["cartons"] == 109
    assert after["opening"]["packs"] == 5
    assert after["opening"]["pieces"] == 0
    assert after["production"]["base_qty"] == 0
    assert after["return_"]["base_qty"] == 0
    assert after["issued"]["base_qty"] == 0
    assert after["closing"]["base_qty"] == 10950


def _setup_locals(client, login_as):
    login_as("root", "password123", "super_admin")
    product = _make_product(client, "Compact Standard Hotfix Repro")
    from webapp.models.user import User
    from webapp.models.product import Product
    user = User.query.filter_by(username="root").first()
    prod = _db.session.get(Product, product["id"])
    rule = prod.current_packaging_rule()
    return {"product": product, "user_id": user.id, "rule_id": rule.id}


# =====================================================================
# Earlier movement invalidates a would-be auto-anchor first row (runtime)
# =====================================================================

def _submit_as_operator(client, login_as, pid, date, shift, cartons):
    """Logs in as an Operator (permitted to edit Opening, but never able
    to create/force an anchor — see the existing Stage 8 test
    test_operator_cannot_create_a_new_anchor_on_a_derived_period) and
    submits an arbitrary value that deliberately differs from whatever
    would be derived. If the submission is honored, the row wrongly
    auto-anchored; if it's silently forced to the derived value, the row
    correctly stayed non-override — an unambiguous signal regardless of
    the exact derived number, which is why every "does this stay
    non-override" test below uses this instead of an elevated submission
    (an elevated user submitting a genuinely different value is *always*
    honored as a deliberate correction — see
    test_genuine_manager_correction_remains_an_override below — so it
    can't distinguish 'derived correctly' from 'wrongly auto-anchored'.)"""
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as(f"op_{pid}_{date}_{shift}", "password123", "operator")
    return client.post("/api/daily-figures", json={
        "product_id": pid, "date": date, "shift": shift,
        "opening": {"cartons": cartons, "packs": 0, "pieces": 0},
    })


def test_earlier_production_invalidates_a_later_first_row_override(client, setup, login_as):
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-07-19", "Day", 20)
    res = _submit_as_operator(client, login_as, pid, "2026-08-01", "Day", 999)
    assert res.status_code == 200
    figure = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first()
    assert figure.opening_stock_is_override is False
    assert res.get_json()["opening"]["cartons"] == 20  # derived, not the submitted 999


def test_earlier_returns_invalidates_a_later_first_row_override(client, setup, login_as):
    pid = setup["product"]["id"]
    _finalize_return(client, pid, "2026-07-19", 4)
    res = _submit_as_operator(client, login_as, pid, "2026-08-01", "Day", 999)
    figure = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first()
    assert figure.opening_stock_is_override is False
    assert res.get_json()["opening"]["cartons"] == 4


def test_earlier_dispatch_invalidates_a_later_first_row_override(client, setup, login_as):
    pid = setup["product"]["id"]
    # A dispatch that drives the balance negative still counts as real
    # finalized movement — the point is any movement at all, before a
    # product's first-ever DailyFigure row, must prevent auto-anchoring.
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-19", "Day", 4, "HF-1")
    res = _submit_as_operator(client, login_as, pid, "2026-08-01", "Day", 999)
    figure = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first()
    assert figure.opening_stock_is_override is False
    assert res.get_json()["opening"]["base_qty"] == -400


def test_same_date_day_movement_ordered_before_night(client, setup, login_as):
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-07-19", "Day", 5)
    res = _submit_as_operator(client, login_as, pid, "2026-07-19", "Night", 999)
    figure = DailyFigure.query.filter_by(product_id=pid, date="2026-07-19", shift="Night").first()
    assert figure.opening_stock_is_override is False  # Day production precedes this Night period
    assert res.get_json()["opening"]["cartons"] == 5


def test_night_movement_carries_to_next_day(client, setup, login_as):
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-07-19", "Night", 6)
    res = _submit_as_operator(client, login_as, pid, "2026-07-20", "Day", 999)
    figure = DailyFigure.query.filter_by(product_id=pid, date="2026-07-20", shift="Day").first()
    assert figure.opening_stock_is_override is False
    assert res.get_json()["opening"]["cartons"] == 6


# =====================================================================
# Genuine anchors and corrections still work exactly as before
# =====================================================================

def test_genuine_initial_row_before_all_movement_remains_an_override(client, setup):
    pid = setup["product"]["id"]
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    figure = DailyFigure.query.filter_by(product_id=pid, date="2026-07-01", shift="Day").first()
    assert figure.opening_stock_is_override is True
    assert res.get_json()["opening"]["cartons"] == 10


def test_genuine_manager_correction_remains_an_override(client, login_as):
    login_as("root", "password123", "super_admin")
    product = _make_product(client, "Hotfix Manager Correction")
    pid = product["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    login_as("mgr_hotfix", "password123", "manager")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-15", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "physical count",
    })
    assert res.status_code == 200
    assert res.get_json()["opening"]["cartons"] == 500
    figure = DailyFigure.query.filter_by(product_id=pid, date="2026-07-15", shift="Day").first()
    assert figure.opening_stock_is_override is True


def test_genuine_super_admin_correction_remains_an_override(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-15", "shift": "Day",
        "opening": {"cartons": 777, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "physical count",
    })
    assert res.status_code == 200
    figure = DailyFigure.query.filter_by(product_id=pid, date="2026-07-15", shift="Day").first()
    assert figure.opening_stock_is_override is True


def test_elevated_no_op_save_does_not_create_an_override(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    figure = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first()
    assert figure.opening_stock_is_override is False


def test_reset_created_zero_rows_are_not_overrides(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-15", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-reset", json={
        "date": "2026-07-15", "shift": "Day", "product_id": pid, "reason": "test reset",
    })
    figure = DailyFigure.query.filter_by(product_id=pid, date="2026-07-15", shift="Day").first()
    assert figure.opening_stock_is_override is False


def test_no_activity_rows_are_not_overrides(client, login_as, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_hotfix", "password123", "operator")
    res = client.post("/api/daily-entry-status/no-activity", json={
        "product_id": pid, "date": "2026-07-10", "shift": "Day",
    })
    assert res.status_code == 200
    assert DailyFigure.query.filter_by(product_id=pid, date="2026-07-10").count() == 0


# =====================================================================
# Data safety: nothing destroyed, No Activity status survives derivation
# =====================================================================

def test_source_movement_and_rows_survive_the_repair_migration(client, login_as, app):
    setup = _setup_locals(client, login_as)
    pid = setup["product"]["id"]
    rule_id = setup["rule_id"]
    _finalize_production(client, pid, "2026-07-19", "Day", 10)
    row_id = _insert_stale_override_row(app, pid, rule_id, "2026-08-01", "Day", setup["user_id"])

    _run_repair_migration(app)

    with app.app_context():
        row = _db.session.get(DailyFigure, row_id)
        assert row is not None  # never deleted
        assert row.opening_base_qty == 0  # stored quantity fields untouched
        from webapp.models.production_record import ProductionRecord
        assert ProductionRecord.query.filter_by(date="2026-07-19").count() == 1  # source untouched


def test_no_activity_completion_remains_present_and_still_carries_balance(client, login_as, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 100, "packs": 0, "pieces": 0},
    })
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_hotfix2", "password123", "operator")
    client.post("/api/daily-entry-status/no-activity", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
    })
    status = client.get(f"/api/daily-entry-status?product_id={pid}&date=2026-08-01&shift=Day").get_json()
    assert status["status"] in ("no_activity", "completed")
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 100


def test_exact_integer_base_unit_arithmetic_is_unaffected(client, setup, login_as):
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-07-19", "Day", 1, 3, 7)
    res = _submit_as_operator(client, login_as, pid, "2026-08-01", "Day", 999)
    view = res.get_json()
    assert isinstance(view["opening"]["base_qty"], int)
    assert view["opening"]["base_qty"] == 137


# =====================================================================
# Universal application — parameterized across products and packaging
# =====================================================================

@pytest.mark.parametrize("cartons_to_packs,packs_to_pieces,carton_to_pieces,production_qty", [
    (10, 10, None, (2, 3, 4)),   # standard carton+pack+piece
    (None, None, 60, (1, 0, 15)),  # carton+piece, no pack tier (KingMax/JumboMax-style)
    (6, 10, None, (1, 2, 4)),    # Napkin-style mixed radix (6 packs/carton)
    (12, 5, None, (3, 1, 2)),    # another distinct packaging rule
])
def test_first_row_after_movement_derives_correctly_for_every_packaging_rule(
    client, login_as, cartons_to_packs, packs_to_pieces, carton_to_pieces, production_qty
):
    login_as("root", "password123", "super_admin")
    product = _make_product(
        client, f"Hotfix Packaging {cartons_to_packs}-{packs_to_pieces}-{carton_to_pieces}",
        cartons_to_packs=cartons_to_packs, packs_to_pieces=packs_to_pieces, carton_to_pieces=carton_to_pieces,
    )
    pid = product["id"]
    c, p, pc = production_qty
    _finalize_production(client, pid, "2026-07-19", "Day", c, p, pc)

    res = _submit_as_operator(client, login_as, pid, "2026-08-01", "Day", 999999)
    assert res.status_code == 200
    figure = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first()
    assert figure.opening_stock_is_override is False
    assert res.get_json()["opening"]["cartons"] == c
    assert res.get_json()["opening"]["packs"] == p
    assert res.get_json()["opening"]["pieces"] == pc


def test_every_product_carries_its_own_independent_balance(client, login_as):
    login_as("root", "password123", "super_admin")
    products = [_make_product(client, f"Hotfix Independent {i}") for i in range(4)]
    for i, product in enumerate(products):
        _finalize_production(client, product["id"], "2026-07-19", "Day", 10 * (i + 1))

    for i, product in enumerate(products):
        view = _view(client, product["id"], "2026-08-01")
        assert view["opening"]["cartons"] == 10 * (i + 1)  # never another product's movement


def test_no_hard_coded_product_logic_arbitrary_names_behave_identically(client, login_as):
    """The carry-forward/anchor rules must not special-case any product
    name — verified by running the exact same scenario against products
    named after every current product family, with identical results."""
    login_as("root", "password123", "super_admin")
    names = [
        "Compact Standard", "Compact Corporate", "Lavex", "Premium", "Mambo",
        "Silky 4pack", "Napkins Corporate", "Napkins Standard", "Napkins Damage",
        "KingMax", "JumboMax", "Straws", "Kitchen Towel Doubles",
    ]
    for name in names:
        # Re-elevate before each admin product-create call without
        # re-registering "root" (login_as() always tries to create the
        # user and would collide on the unique username constraint).
        client.post("/api/login", json={"username": "root", "password": "password123"})
        product = _make_product(client, f"{name} Hotfix Universal")
        pid = product["id"]
        _finalize_production(client, pid, "2026-07-19", "Day", 7)
        res = _submit_as_operator(client, login_as, pid, "2026-08-01", "Day", 999)
        figure = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first()
        assert figure.opening_stock_is_override is False, name
        assert res.get_json()["opening"]["cartons"] == 7, name
