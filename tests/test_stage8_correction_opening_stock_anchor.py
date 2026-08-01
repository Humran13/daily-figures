"""
Stage 8 correction — "Opening Balance -> Opening Stock rename regression"
investigation and fix.

Investigation summary (see the completion report delivered alongside this
file for the full 14-item writeup): there was never a rename. Git history
(`git log --all -p -S"opening_balance"` etc.) proves the internal field has
always been `opening_base_qty` / `opening_cartons` / `opening_packs` /
`opening_pieces`; "Opening Balance" only ever existed as a forbidden-label
guard string in a frontend-wording test, added in the same commit that
introduced "Opening Stock" as the display label. There is no property-name
mismatch, no `|| 0` / `.get(..., 0)` fallback, and no separate response
path for completed/No-Activity periods — `daily_figure_view()` is the sole
source for every caller (Daily Figures, History, exports) and never even
imports DailyEntryStatus.

The real root cause, proven with a live reproduction: `daily_figure_view()`
used to trust ANY DailyFigure row sitting exactly at the viewed date/shift
as an authoritative Opening Stock value, forever — regardless of how that
row came to exist (a stale/zero row, an elevated user's no-op resave of an
already-correct pre-filled value, a Reset-cleared row, bad legacy-shaped
staging data). The fix is `DailyFigure.opening_stock_is_override`: a row's
stored opening is only ever trusted when this flag is True (a genuine
first-ever entry, or an elevated correction that actually differs from
derivation). Every other row is invisible to Opening Stock derivation, the
same as if it didn't exist.

This file exercises that fix directly against the reported staging
scenario and the specific failure modes named in the correction request,
through the same `/api/daily-figures` endpoint the browser uses.
"""
from pathlib import Path

from webapp.extensions import db as _db
from webapp.models.daily_figure import DailyFigure
from webapp.models.product import Product

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _setup(client, login_as, packaging=(10, 10), name="Compact Standard Correction"):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": packaging[0], "packs_to_pieces": packaging[1],
    })
    category = client.post("/api/admin/sales-categories", json={"name": f"{name} Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": f"{name} Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "customer": customer}


def _finalize_production(client, product_id, date_str, shift, cartons, packs=0, pieces=0):
    p = client.post("/api/production", json={
        "date": date_str, "shift": shift,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    res = client.post(f"/api/production/{p['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return p


def _view(client, product_id, date_str, shift="Day"):
    return client.get(f"/api/daily-figures/{product_id}?date={date_str}&shift={shift}").get_json()


# =====================================================================
# The exact reported staging scenario, Compact Standard, 19 July -> 1 August
# =====================================================================

def test_exact_repro_stale_zero_row_at_target_does_not_freeze_opening(client, login_as, app):
    """This is the literal reported bug: 19 July Day — Opening 0, Production
    109.50 Ctns (109 cartons, 5 packs, 0 pieces under a 10/10 packaging
    rule), Returns 0, Issued 0, Closing 109.50 Ctns. 1 August Day has no
    intervening movement, but — as staging actually had — a stale
    DailyFigure row already exists at 1 August with a zero, non-override
    Opening (exactly the shape a pre-fix save, a stuck reset, or bad
    migrated data could leave behind). Opening Stock for 1 August must
    still be 109.50 Ctns / 10950 base units, not 0."""
    setup = _setup(client, login_as)
    pid = setup["product"]["id"]

    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-19", "Day", 109, 5, 0)

    closing_19 = _view(client, pid, "2026-07-19")["closing"]
    assert closing_19["cartons"] == 109 and closing_19["packs"] == 5 and closing_19["pieces"] == 0
    assert closing_19["base_qty"] == 10950

    # Simulate the stale/bad row staging actually had at the target date.
    with app.app_context():
        product = _db.session.get(Product, pid)
        rule = product.current_packaging_rule()
        stale = DailyFigure(
            product_id=pid, date="2026-08-01", shift="Day",
            opening_cartons=0, opening_packs=0, opening_pieces=0, opening_base_qty=0,
            opening_stock_is_override=False,
            return_cartons=0, return_packs=0, return_pieces=0, return_base_qty=0,
            production_cartons=0, production_packs=0, production_pieces=0, production_base_qty=0,
            packaging_rule_id=rule.id, created_by=1, updated_by=1,
        )
        _db.session.add(stale)
        _db.session.commit()

    view_aug1 = _view(client, pid, "2026-08-01")
    assert view_aug1["opening"]["base_qty"] == 10950
    assert view_aug1["opening"]["cartons"] == 109
    assert view_aug1["opening"]["packs"] == 5
    assert view_aug1["opening"]["pieces"] == 0
    assert view_aug1["closing"]["base_qty"] == 10950


def test_elevated_resave_of_already_correct_prefilled_value_does_not_freeze_opening(client, login_as):
    """The other real mechanism behind the staging bug: an elevated user
    (Manager/Super Admin) pages through Daily Figures and saves a period
    whose Opening field was pre-filled with the already-correct derived
    value, without intending a correction. That save must NOT become a new
    anchor — a later upstream correction must still ripple through it."""
    setup = _setup(client, login_as)
    pid = setup["product"]["id"]

    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 100, "packs": 0, "pieces": 0},
    })
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 100

    # Elevated no-op resave: submits exactly what's already derived.
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 100, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    with_stale_row = DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").first()
    assert with_stale_row is not None
    assert with_stale_row.opening_stock_is_override is False

    # A later, real correction to the anchor's own upstream production.
    _finalize_production(client, pid, "2026-07-19", "Day", 10)
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 110


def test_no_activity_after_stale_view_still_carries_correct_balance(client, login_as):
    setup = _setup(client, login_as)
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 100, "packs": 0, "pieces": 0},
    })
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_stale", "password123", "operator")
    res = client.post("/api/daily-entry-status/no-activity", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
    })
    assert res.status_code == 200
    assert DailyFigure.query.filter_by(product_id=pid, date="2026-08-01", shift="Day").count() == 0
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 100


# =====================================================================
# Completed / No-Activity responses use the exact same live calculation
# =====================================================================

def test_completed_status_does_not_freeze_or_bypass_derived_calculation(client, login_as):
    setup = _setup(client, login_as)
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_complete", "password123", "operator")
    client.post("/api/daily-entry-status/no-activity", json={
        "product_id": pid, "date": "2026-07-15", "shift": "Day",
    })
    status = client.get(f"/api/daily-entry-status?product_id={pid}&date=2026-07-15&shift=Day").get_json()
    assert status["status"] in ("no_activity", "completed")

    _finalize_production(client, pid, "2026-07-05", "Day", 25)
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 35


def test_normal_and_no_activity_dates_return_identical_response_shape(client, login_as):
    setup = _setup(client, login_as)
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_shape", "password123", "operator")
    client.post("/api/daily-entry-status/no-activity", json={
        "product_id": pid, "date": "2026-07-10", "shift": "Day",
    })
    normal_view = _view(client, pid, "2026-07-05")
    no_activity_view = _view(client, pid, "2026-07-10")
    assert set(normal_view.keys()) == set(no_activity_view.keys())
    assert set(normal_view["opening"].keys()) == set(no_activity_view["opening"].keys())


# =====================================================================
# Legitimate zero remains a valid value, missing never silently becomes 0
# =====================================================================

def test_legitimate_zero_opening_is_preserved_and_carried_forward(client, login_as):
    setup = _setup(client, login_as)
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    figure = DailyFigure.query.filter_by(product_id=pid, date="2026-07-01", shift="Day").first()
    assert figure.opening_stock_is_override is True  # a genuine first-ever entry, even though it's 0
    assert _view(client, pid, "2026-07-01")["opening"]["base_qty"] == 0
    assert _view(client, pid, "2026-08-01")["opening"]["base_qty"] == 0

    _finalize_production(client, pid, "2026-07-05", "Day", 3)
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 3


def test_response_opening_key_always_present_never_silently_dropped(client, login_as):
    """A view for a product with a packaging rule always has an 'opening'
    key with a 'base_qty' — never omitted in a way that a `|| 0` /
    `.get(..., 0)`-style read on the frontend could mistake for a
    legitimate zero."""
    setup = _setup(client, login_as)
    pid = setup["product"]["id"]
    view = _view(client, pid, "2026-07-01")  # no DailyFigure row exists at all yet
    assert "opening" in view
    assert view["opening"] is not None
    assert "base_qty" in view["opening"]
    assert view["opening"]["base_qty"] == 0  # a genuine, explicit zero — not a missing property


# =====================================================================
# Dashboard / Daily Figures / History / exports agree on the same Opening
# =====================================================================

def test_dashboard_and_daily_figures_agree_on_opening_stock(client, login_as, app):
    setup = _setup(client, login_as)
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 20, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-05", "Day", 8)

    daily_figures_opening = _view(client, pid, "2026-08-01")["opening"]["base_qty"]
    with app.app_context():
        from webapp.services.stock_service import opening_base_qty_at
        dashboard_opening = opening_base_qty_at(pid, "2026-08-01", "Day")
    assert daily_figures_opening == dashboard_opening == 2800


def test_history_view_agrees_with_daily_figures_for_the_same_row(client, login_as):
    """History (webapp/routes/daily_figures.py's history()) queries
    DailyFigure rows directly but serializes each one through the exact
    same daily_figure_view() as the live Daily Figures endpoint — proving
    there's no second, separately-derived serializer for historical rows
    that could go stale."""
    setup = _setup(client, login_as)
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-10", "Day", 2)

    live_view = _view(client, pid, "2026-07-01")
    history_res = client.get(f"/api/daily-figures/history?product_id={pid}&date=2026-07-01&shift=Day")
    assert history_res.status_code == 200
    history_rows = history_res.get_json()
    assert len(history_rows) == 1
    assert history_rows[0]["opening"]["base_qty"] == live_view["opening"]["base_qty"] == 500


# =====================================================================
# Migration safety — existing values survive, no duplicate/fake rows
# =====================================================================

def test_existing_db_opening_values_are_not_altered_by_the_new_column(client, login_as, app):
    setup = _setup(client, login_as)
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 12, "packs": 3, "pieces": 4},
    })
    with app.app_context():
        figure = DailyFigure.query.filter_by(product_id=pid, date="2026-07-01", shift="Day").first()
        assert figure.opening_cartons == 12
        assert figure.opening_packs == 3
        assert figure.opening_pieces == 4
        assert figure.opening_base_qty == 1234


def test_no_duplicate_or_fake_rows_created_while_reproducing_the_bug(client, login_as):
    setup = _setup(client, login_as)
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 100, "packs": 0, "pieces": 0},
    })
    for _ in range(3):
        _view(client, pid, "2026-08-01")
    rows = DailyFigure.query.filter_by(product_id=pid).all()
    assert len(rows) == 1


# =====================================================================
# Static frontend contract — JS reads the exact property the API returns
# =====================================================================

def test_frontend_reads_the_canonical_opening_property_the_api_returns():
    """The API's daily_figure_view() (webapp/services/stock_service.py)
    returns an "opening" object with base_qty/cartons/packs/pieces, and an
    "opening_editable" boolean alongside it — no other opening-shaped key.
    This is a static guard against the frontend silently drifting onto a
    different/legacy property name."""
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "view.opening" in index_html
    assert "view.opening_editable" in index_html
    # Guard against regressions back toward a stale/legacy property name.
    for forbidden in ("view.opening_balance", "view.openingBalance", "view.opening_base",
                       "view.prior_closing", "view.priorBalance"):
        assert forbidden not in index_html
