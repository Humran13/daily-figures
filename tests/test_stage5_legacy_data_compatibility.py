"""
Stage 5 final data-compatibility check: pre-existing Daily Figures records
(created before Returns/Production became their own Books) stored Return
and Production directly on the DailyFigure row itself, via the old
upsert_daily_figure() that accepted `return_`/`production` in its payload.
That write path no longer exists (see webapp/routes/daily_figures.py's
upsert(), which now only accepts `opening`), but the DailyFigure columns
themselves (`return_base_qty`, `production_base_qty`, etc.) were never
touched by the Stage 5 migration — it only adds new tables.

These tests simulate that "already on disk before Stage 5" data directly
via the ORM (the same way webapp/services/legacy_migration.py's older
legacy-decode path writes a DailyFigure row directly, bypassing the API),
then confirm:

  1. Both legacy Production AND legacy Returns are preserved, not just
     Returns (the original completion report only explicitly confirmed
     Returns).
  2. Opening/Closing Stock computed from that legacy row is byte-for-byte
     identical to what the pre-Stage-5 formula would have produced, as
     long as no NEW finalized Returns/Production Book record exists for
     that same date (i.e. deploying Stage 5 alone changes nothing).
  3. Adding a NEW finalized Returns/Production Book entry for the SAME
     date/shift as an existing legacy row correctly ADDS to the legacy
     baseline rather than replacing or double-counting it — the legacy
     value and the book value are two genuinely separate contributions
     (one recorded before this stage existed, one recorded through the
     new dedicated workflow), never the same physical event counted twice.
  4. date_range_summary (used by the summary report and the dashboard)
     follows the same rule across a range of legacy + new-book dates.

Final aggregation formulas (see webapp/services/stock_service.py):
    Return   = legacy_stored_return_base_qty
               + (finalized Returns Book total for that date, only if shift == Day)
    Production = legacy_stored_production_base_qty
               + (finalized Production Book total for that exact date+shift)
    Closing Stock = Opening Stock + Return + Production - Issued
"""
import pytest

from webapp.extensions import db
from webapp.models.daily_figure import DailyFigure
from webapp.models.product import Product
from webapp.models.user import User


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin, app):
    product = client.post("/api/admin/products", json={"name": "Legacy Compat Test"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return {"product": product}


def _insert_legacy_daily_figure(app, product_id, date, shift, *, opening, return_base, production_base):
    """Directly constructs a DailyFigure row exactly as the pre-Stage-5
    upsert_daily_figure() (or the older legacy-decode migration path) would
    have left on disk — bypassing the current API, which no longer accepts
    return_/production at all."""
    with app.app_context():
        product = db.session.get(Product, product_id)
        rule = product.current_packaging_rule()
        user = User.query.filter_by(username="root").first()
        figure = DailyFigure(
            product_id=product_id, date=date, shift=shift,
            opening_cartons=0, opening_packs=0, opening_pieces=0, opening_base_qty=opening,
            return_cartons=0, return_packs=0, return_pieces=0, return_base_qty=return_base,
            production_cartons=0, production_packs=0, production_pieces=0, production_base_qty=production_base,
            packaging_rule_id=rule.id, created_by=user.id, updated_by=user.id,
        )
        db.session.add(figure)
        db.session.commit()
        return figure.id


def _finalize_return(client, product_id, date, cartons=0, packs=0, pieces=0):
    created = client.post("/api/returns", json={
        "date": date, "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    return created


def _finalize_production(client, product_id, date, shift, cartons=0, packs=0, pieces=0):
    created = client.post("/api/production", json={
        "date": date, "shift": shift,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    return created


def _view(client, product_id, date, shift):
    return client.get(f"/api/daily-figures/{product_id}?date={date}&shift={shift}").get_json()


def _pre_stage5_closing(opening, return_base, production_base, issued):
    """The exact pre-Stage-5 formula: opening + return + production - issued,
    with return_/production read straight off the stored row."""
    return opening + return_base + production_base - issued


# ---------- 1 & 2: legacy Production AND Returns both preserved unchanged ----------

def test_legacy_production_value_is_preserved_after_stage5(client, setup, app):
    pid = setup["product"]["id"]
    _insert_legacy_daily_figure(app, pid, "2026-06-01", "Night",
                                 opening=500, return_base=0, production_base=250)

    view = _view(client, pid, "2026-06-01", "Night")
    assert view["production"]["base_qty"] == 250
    assert view["production"]["legacy"] == 250
    assert view["production"]["from_production_book"] == 0


def test_legacy_return_value_is_preserved_after_stage5(client, setup, app):
    pid = setup["product"]["id"]
    _insert_legacy_daily_figure(app, pid, "2026-06-01", "Day",
                                 opening=500, return_base=75, production_base=0)

    view = _view(client, pid, "2026-06-01", "Day")
    assert view["return_"]["base_qty"] == 75
    assert view["return_"]["legacy"] == 75
    assert view["return_"]["from_returns_book"] == 0


def test_legacy_night_return_is_preserved_even_though_new_returns_are_day_only(client, setup, app):
    """The old workflow allowed entering a Return value on any shift; the
    new Returns Book is Day-only going forward. A pre-existing Night Return
    value must not be discarded or reinterpreted by that new restriction."""
    pid = setup["product"]["id"]
    _insert_legacy_daily_figure(app, pid, "2026-06-01", "Night",
                                 opening=500, return_base=40, production_base=0)

    view = _view(client, pid, "2026-06-01", "Night")
    assert view["return_"]["base_qty"] == 40


def test_legacy_opening_and_closing_stock_unchanged_by_stage5_alone(client, setup, app):
    """With no new Returns/Production Book activity at all for this date,
    Opening Stock and Closing Stock must compute to exactly the same
    numbers Stage 4 would have produced — deploying Stage 5 changes
    nothing about existing history by itself."""
    pid = setup["product"]["id"]
    opening, return_base, production_base = 1000, 50, 100
    _insert_legacy_daily_figure(app, pid, "2026-06-01", "Day",
                                 opening=opening, return_base=return_base, production_base=production_base)

    expected_closing = _pre_stage5_closing(opening, return_base, production_base, issued=0)
    view = _view(client, pid, "2026-06-01", "Day")
    assert view["opening"]["base_qty"] == opening
    assert view["closing"]["base_qty"] == expected_closing


def test_legacy_row_with_both_production_and_return_populated(client, setup, app):
    """A single pre-Stage-5 record containing both a manually entered
    Production AND Return value, as the correction explicitly asks for."""
    pid = setup["product"]["id"]
    _insert_legacy_daily_figure(app, pid, "2026-06-01", "Day",
                                 opening=2000, return_base=30, production_base=150)

    view = _view(client, pid, "2026-06-01", "Day")
    assert view["return_"]["base_qty"] == 30
    assert view["production"]["base_qty"] == 150
    assert view["closing"]["base_qty"] == 2000 + 30 + 150 - 0


def test_editing_opening_on_a_legacy_row_does_not_disturb_its_legacy_return_or_production(client, setup, app):
    """Saving a new Opening Stock value (the one thing Daily Figures still
    accepts directly) on a pre-existing row must leave its legacy
    return_base_qty/production_base_qty completely untouched — the upsert
    route no longer has any code path that writes either column."""
    pid = setup["product"]["id"]
    fig_id = _insert_legacy_daily_figure(app, pid, "2026-06-02", "Day",
                                          opening=100, return_base=20, production_base=40)

    # Opening isn't "editable" here since a prior close doesn't exist for an
    # even-earlier date, so this is the first entry for this product's
    # timeline and opening_editable is True — but even so, saving must not
    # touch return_/production.
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-06-02", "shift": "Day",
        "opening": {"cartons": 2, "packs": 0, "pieces": 0},  # 200
    })

    with app.app_context():
        figure = db.session.get(DailyFigure, fig_id)
        assert figure.return_base_qty == 20
        assert figure.production_base_qty == 40


# ---------- 3: new book records ADD to (never replace/double-count) the legacy baseline ----------

def test_new_finalized_return_adds_to_legacy_return_without_replacing_it(client, setup, app):
    pid = setup["product"]["id"]
    _insert_legacy_daily_figure(app, pid, "2026-06-03", "Day",
                                 opening=500, return_base=50, production_base=0)

    _finalize_return(client, pid, "2026-06-03", cartons=1)  # +100 via the new book

    view = _view(client, pid, "2026-06-03", "Day")
    assert view["return_"]["legacy"] == 50
    assert view["return_"]["from_returns_book"] == 100
    assert view["return_"]["base_qty"] == 150  # additive, not overwritten, not doubled


def test_new_finalized_production_adds_to_legacy_production_without_replacing_it(client, setup, app):
    pid = setup["product"]["id"]
    _insert_legacy_daily_figure(app, pid, "2026-06-03", "Night",
                                 opening=500, return_base=0, production_base=70)

    _finalize_production(client, pid, "2026-06-03", "Night", cartons=2)  # +200 via the new book

    view = _view(client, pid, "2026-06-03", "Night")
    assert view["production"]["legacy"] == 70
    assert view["production"]["from_production_book"] == 200
    assert view["production"]["base_qty"] == 270


def test_no_double_counting_when_a_return_book_entry_is_voided(client, setup, app):
    """Voiding the NEW book entry must drop it back out, leaving exactly
    the legacy baseline — proving the two are summed live, not merged into
    a single stored number that could get stuck."""
    pid = setup["product"]["id"]
    _insert_legacy_daily_figure(app, pid, "2026-06-03", "Day",
                                 opening=500, return_base=50, production_base=0)
    created = _finalize_return(client, pid, "2026-06-03", cartons=1)

    assert _view(client, pid, "2026-06-03", "Day")["return_"]["base_qty"] == 150

    client.post(f"/api/returns/{created['id']}/void", json={"reason": "duplicate"})
    assert _view(client, pid, "2026-06-03", "Day")["return_"]["base_qty"] == 50


def test_closing_stock_correctly_combines_legacy_and_new_book_contributions(client, setup, app):
    pid = setup["product"]["id"]
    _insert_legacy_daily_figure(app, pid, "2026-06-04", "Day",
                                 opening=1000, return_base=20, production_base=30)
    _finalize_return(client, pid, "2026-06-04", packs=1)        # +10
    _finalize_production(client, pid, "2026-06-04", "Day", pieces=5)  # +5

    view = _view(client, pid, "2026-06-04", "Day")
    assert view["return_"]["base_qty"] == 30       # 20 legacy + 10 new
    assert view["production"]["base_qty"] == 35    # 30 legacy + 5 new
    assert view["closing"]["base_qty"] == 1000 + 30 + 35 - 0


# ---------- 4: date-range summary follows the same rule ----------

def test_date_range_summary_combines_legacy_and_book_contributions_without_duplication(client, setup, app):
    pid = setup["product"]["id"]
    _insert_legacy_daily_figure(app, pid, "2026-06-05", "Day",
                                 opening=1000, return_base=40, production_base=60)
    _finalize_return(client, pid, "2026-06-05", cartons=1)               # +100
    _finalize_production(client, pid, "2026-06-05", "Day", cartons=2)    # +200

    res = client.get("/api/reports/summary?date_from=2026-06-05&date_to=2026-06-05")
    row = next(r for r in res.get_json() if r["product_id"] == pid)
    assert row["return_base_qty"] == 140      # 40 legacy + 100 book
    assert row["production_base_qty"] == 260  # 60 legacy + 200 book
    assert row["closing_base_qty"] == 1000 + 140 + 260 - 0


def test_date_range_summary_surfaces_a_date_with_only_new_book_activity_and_no_legacy_row(client, setup, app):
    """A date with a finalized Return/Production but no DailyFigure row at
    all (pure Stage-5-era data, no legacy baseline) must still be counted —
    covers the summary's "skip only if truly nothing happened" guard."""
    pid = setup["product"]["id"]
    _finalize_return(client, pid, "2026-06-06", cartons=1)

    res = client.get("/api/reports/summary?date_from=2026-06-06&date_to=2026-06-06")
    row = next((r for r in res.get_json() if r["product_id"] == pid), None)
    assert row is not None
    assert row["return_base_qty"] == 100
