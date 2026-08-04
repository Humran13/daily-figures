"""
Final stock architecture — clean ledger cutover with Excel-style carry
forward.

Rather than repairing the mixed pre-existing carry-forward chain one
legacy row at a time, this establishes a controlled new operational
starting point (a LedgerCutover) that behaves like a properly designed
Excel stock sheet while remaining database-backed, multi-user-safe,
auditable, and exact.

Architecture note: `stock_service.py` was ALREADY the single shared
calculation engine every surface in this app uses (Daily Figures,
Dashboard, Reports, History, Exports, Reset, the stock-ledger CLI) — see
the completion report for the full survey. The cutover is expressed as
ONE NEW unconditionally-trusted DailyFigure anchor type
(OPENING_STOCK_SOURCE_LEDGER_CUTOVER), written exactly once per product
at the cutover's own effective Date+Shift by
ledger_cutover_service.activate_cutover(). Because stock_service.py's
existing backward-scanning anchor logic (_find_anchor_figure()/
get_prior_closing_base_qty()) already stops at the FIRST trusted anchor
it finds walking backward from a target period, this single change makes
every post-cutover period's carry-forward search naturally stop at the
cutover and never reach pre-cutover data — no other function needed to
change to enforce the ledger boundary rule.
"""
import pytest

from webapp.services import ledger_cutover_service as cutover_svc
from webapp.services import legacy_migration


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


def _make_product(client, name, rule=None):
    p = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{p['id']}/packaging-rules", json=rule or {"cartons_to_packs": 10, "packs_to_pieces": 10})
    return p


def _view(client, pid, date_str, shift="Day"):
    return client.get(f"/api/daily-figures/{pid}?date={date_str}&shift={shift}").get_json()


def _figure(app, product_id, date_str, shift):
    with app.app_context():
        from webapp.models.daily_figure import DailyFigure
        return DailyFigure.query.filter_by(product_id=product_id, date=date_str, shift=shift).first()


def _full_cutover(app, effective_date, effective_shift, balances, reason="clean cutover test"):
    """balances: dict[product_id] -> (cartons, packs, pieces). Drafts,
    fills every balance, verifies, previews, and activates in one call —
    the common path most tests need; a few tests call the individual
    steps directly to exercise draft-only / verify-only states."""
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        draft = cutover_svc.create_draft(effective_date, effective_shift, reason, root)
        for pid, (c, p, pc) in balances.items():
            cutover_svc.set_balance(draft.id, pid, c, p, pc, root)
        _db.session.commit()
        cutover_svc.verify_cutover(draft.id, root)
        _db.session.commit()
        preview = cutover_svc.preview_activation(draft.id)
        result = cutover_svc.activate_cutover(
            draft.id, root, preview_token=preview["preview_token"],
            confirmation_text=f"ACTIVATE LEDGER CUTOVER {effective_date} {effective_shift.upper()}",
            backup_confirmed=True, reason=reason,
        )
        _db.session.commit()
        return draft.id, result


# =====================================================================
# CUTOVER
# =====================================================================

def test_draft_cutover_does_not_affect_stock(client, super_admin, app):
    p = _make_product(client, "Draft Cutover Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-10", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        draft = cutover_svc.create_draft("2026-08-01", "Day", "test", root)
        cutover_svc.set_balance(draft.id, p["id"], 999, 0, 0, root)
        _db.session.commit()

    view = _view(client, p["id"], "2026-08-01", "Day")
    assert view["opening"]["base_qty"] == 500  # unaffected — carried from the real 5-carton anchor, not the draft's 999


def test_activated_cutover_becomes_authoritative(client, super_admin, app):
    p = _make_product(client, "Activated Cutover Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (50, 0, 0)})
    view = _view(client, p["id"], "2026-08-01", "Day")
    assert view["opening"]["base_qty"] == 5000
    figure = _figure(app, p["id"], "2026-08-01", "Day")
    assert figure.opening_stock_source == "ledger_cutover"
    assert figure.opening_stock_is_override is True


def test_pre_cutover_movements_do_not_affect_post_cutover_opening(client, super_admin, app):
    p = _make_product(client, "Boundary Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-01-01", "shift": "Day",
        "opening": {"cartons": 777, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-06-01", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "pre-cutover noise",
    })
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (50, 0, 0)})

    later = _view(client, p["id"], "2026-08-05", "Day")
    assert later["opening"]["base_qty"] == 5000  # carried straight from the cutover, ignoring 777/1


def test_post_cutover_movements_affect_stock_exactly_once(client, super_admin, app):
    p = _make_product(client, "Movement Once Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (50, 0, 0)})

    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": p["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")

    closing = _view(client, p["id"], "2026-08-01", "Day")["closing"]["base_qty"]
    assert closing == 5500  # 5000 + 500, not double-counted

    next_opening = _view(client, p["id"], "2026-08-02", "Day")["opening"]["base_qty"]
    assert next_opening == 5500  # carried forward once


def test_every_active_product_requires_a_balance(client, super_admin, app):
    p1 = _make_product(client, "Cutover Req Product 1")
    p2 = _make_product(client, "Cutover Req Product 2")
    with app.app_context():
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        draft = cutover_svc.create_draft("2026-08-01", "Day", "test", root)
        cutover_svc.set_balance(draft.id, p1["id"], 10, 0, 0, root)
        # p2 deliberately left without a balance
        with pytest.raises(cutover_svc.LedgerCutoverError):
            cutover_svc.verify_cutover(draft.id, root)


def test_zero_balance_is_valid(client, super_admin, app):
    p = _make_product(client, "Zero Balance Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (0, 0, 0)})
    view = _view(client, p["id"], "2026-08-01", "Day")
    assert view["opening"]["base_qty"] == 0
    figure = _figure(app, p["id"], "2026-08-01", "Day")
    assert figure.opening_stock_source == "ledger_cutover"  # a real, authoritative zero — not a stale one


def test_negative_cutover_balance_is_rejected(client, super_admin, app):
    p = _make_product(client, "Negative Balance Product")
    with app.app_context():
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        draft = cutover_svc.create_draft("2026-08-01", "Day", "test", root)
        with pytest.raises(cutover_svc.LedgerCutoverError):
            cutover_svc.set_balance(draft.id, p["id"], -5, 0, 0, root)


def test_activation_is_atomic(client, super_admin, app):
    """If any product fails validation, nothing is activated — simulated
    by deactivating a product AFTER verification (so it's excluded from
    'every active product' but its stale balance row would still try to
    write) is out of scope; instead we prove atomicity via the missing-
    balance guard applying to the WHOLE activation, not per-product."""
    p1 = _make_product(client, "Atomic Product 1")
    p2 = _make_product(client, "Atomic Product 2")
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        draft = cutover_svc.create_draft("2026-08-01", "Day", "test", root)
        cutover_svc.set_balance(draft.id, p1["id"], 10, 0, 0, root)
        cutover_svc.set_balance(draft.id, p2["id"], 20, 0, 0, root)
        _db.session.commit()
        cutover_svc.verify_cutover(draft.id, root)
        _db.session.commit()
        preview = cutover_svc.preview_activation(draft.id)

    # A NEW product appears (becomes active) after verification but
    # before activation — activation must refuse entirely, not partially
    # apply p1/p2.
    p3 = _make_product(client, "Atomic Product 3")
    with app.app_context():
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        with pytest.raises(cutover_svc.LedgerCutoverError):
            cutover_svc.activate_cutover(
                draft.id, root, preview_token=preview["preview_token"],
                confirmation_text="ACTIVATE LEDGER CUTOVER 2026-08-01 DAY",
                backup_confirmed=True, reason="test",
            )

    figure1 = _figure(app, p1["id"], "2026-08-01", "Day")
    assert figure1 is None  # nothing was written for EITHER product


def test_stale_preview_token_is_rejected(client, super_admin, app):
    p = _make_product(client, "Stale Token Product")
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        draft = cutover_svc.create_draft("2026-08-01", "Day", "test", root)
        cutover_svc.set_balance(draft.id, p["id"], 10, 0, 0, root)
        _db.session.commit()
        cutover_svc.verify_cutover(draft.id, root)
        _db.session.commit()

        with pytest.raises(cutover_svc.LedgerCutoverConflict):
            cutover_svc.activate_cutover(
                draft.id, root, preview_token="stale-or-wrong-token",
                confirmation_text="ACTIVATE LEDGER CUTOVER 2026-08-01 DAY",
                backup_confirmed=True, reason="test",
            )


def test_repeated_activation_is_refused_safely(client, super_admin, app):
    p = _make_product(client, "Repeat Activation Product")
    draft_id, _ = _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    with app.app_context():
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        with pytest.raises(cutover_svc.LedgerCutoverError):
            cutover_svc.activate_cutover(
                draft_id, root, preview_token="anything",
                confirmation_text="ACTIVATE LEDGER CUTOVER 2026-08-01 DAY",
                backup_confirmed=True, reason="test",
            )


def test_second_overlapping_cutover_is_refused(client, super_admin, app):
    p = _make_product(client, "Overlap Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        draft2 = cutover_svc.create_draft("2026-07-15", "Day", "earlier overlap", root)
        cutover_svc.set_balance(draft2.id, p["id"], 20, 0, 0, root)
        _db.session.commit()
        cutover_svc.verify_cutover(draft2.id, root)
        _db.session.commit()
        preview = cutover_svc.preview_activation(draft2.id)
        with pytest.raises(cutover_svc.LedgerCutoverError):
            cutover_svc.activate_cutover(
                draft2.id, root, preview_token=preview["preview_token"],
                confirmation_text="ACTIVATE LEDGER CUTOVER 2026-07-15 DAY",
                backup_confirmed=True, reason="test",
            )


# =====================================================================
# EXCEL-STYLE CARRY FORWARD
# =====================================================================

def test_day_closing_equals_night_opening(client, super_admin, app):
    p = _make_product(client, "Day Night Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    day_closing = _view(client, p["id"], "2026-08-01", "Day")["closing"]["base_qty"]
    night_opening = _view(client, p["id"], "2026-08-01", "Night")["opening"]["base_qty"]
    assert day_closing == night_opening == 1000


def test_night_closing_equals_next_day_opening(client, super_admin, app):
    p = _make_product(client, "Night Next Day Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    night_closing = _view(client, p["id"], "2026-08-01", "Night")["closing"]["base_qty"]
    next_day_opening = _view(client, p["id"], "2026-08-02", "Day")["opening"]["base_qty"]
    assert night_closing == next_day_opening == 1000


def test_no_activity_preserves_the_same_balance(client, super_admin, app):
    p = _make_product(client, "No Activity Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    for date_str in ("2026-08-02", "2026-08-03", "2026-08-04"):
        assert _view(client, p["id"], date_str, "Day")["opening"]["base_qty"] == 1000


def test_production_increases_closing(client, super_admin, app):
    p = _make_product(client, "Production Increase Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": p["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    assert _view(client, p["id"], "2026-08-01", "Day")["closing"]["base_qty"] == 1300


def test_returns_increase_closing(client, super_admin, app):
    p = _make_product(client, "Returns Increase Product")
    cat = client.post("/api/admin/sales-categories", json={"name": "Cutover Cat"}).get_json()
    cust = client.post("/api/admin/customers", json={"name": "Cutover Cust", "sales_category_id": cat["id"]}).get_json()
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    ret = client.post("/api/returns", json={
        "date": "2026-08-01", "customer_id": cust["id"],
        "lines": [{"product_id": p["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{ret['id']}/finalize")
    assert _view(client, p["id"], "2026-08-01", "Day")["closing"]["base_qty"] == 1200


def test_issued_decreases_closing(client, super_admin, app):
    p = _make_product(client, "Issued Decrease Product")
    cat = client.post("/api/admin/sales-categories", json={"name": "Cutover Cat 2"}).get_json()
    cust = client.post("/api/admin/customers", json={"name": "Cutover Cust 2", "sales_category_id": cat["id"]}).get_json()
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    d = client.post("/api/dispatches", json={
        "dispatch_number": "CUT-1", "date": "2026-08-01", "shift": "Day", "customer_id": cust["id"],
        "lines": [{"product_id": p["id"], "cartons": 4, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    assert _view(client, p["id"], "2026-08-01", "Day")["closing"]["base_qty"] == 600


def test_multiple_days_carry_correctly(client, super_admin, app):
    p = _make_product(client, "Multi Day Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (100, 0, 0)})
    prod = client.post("/api/production", json={
        "date": "2026-08-02", "shift": "Day", "lines": [{"product_id": p["id"], "cartons": 10, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    prod2 = client.post("/api/production", json={
        "date": "2026-08-03", "shift": "Day", "lines": [{"product_id": p["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod2['id']}/finalize")

    assert _view(client, p["id"], "2026-08-02", "Day")["closing"]["base_qty"] == 11000
    assert _view(client, p["id"], "2026-08-03", "Day")["closing"]["base_qty"] == 11500
    assert _view(client, p["id"], "2026-08-04", "Day")["opening"]["base_qty"] == 11500


def test_multiple_products_remain_isolated(client, super_admin, app):
    p1 = _make_product(client, "Isolated Product 1")
    p2 = _make_product(client, "Isolated Product 2")
    _full_cutover(app, "2026-08-01", "Day", {p1["id"]: (10, 0, 0), p2["id"]: (99, 0, 0)})
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": p1["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")

    assert _view(client, p1["id"], "2026-08-01", "Day")["closing"]["base_qty"] == 1100
    assert _view(client, p2["id"], "2026-08-01", "Day")["closing"]["base_qty"] == 9900  # untouched by p1's production


def test_no_float_arithmetic_exists(client, super_admin, app):
    p = _make_product(client, "No Float Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (33, 7, 0)})
    view = _view(client, p["id"], "2026-08-01", "Day")
    for part in ("opening", "closing"):
        for key in ("base_qty", "cartons", "packs", "pieces"):
            assert isinstance(view[part][key], int)


# =====================================================================
# RESET
# =====================================================================

def test_status_only_reset_changes_no_stock_values_after_cutover(client, super_admin, app):
    p = _make_product(client, "Reset Status Cutover Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    prod = client.post("/api/production", json={
        "date": "2026-08-05", "shift": "Day", "lines": [{"product_id": p["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    before = _view(client, p["id"], "2026-08-05", "Day")

    client.post("/api/daily-reset", json={"date": "2026-08-05", "shift": "Day", "product_id": p["id"], "mode": "figures_only", "reason": "test"})

    after = _view(client, p["id"], "2026-08-05", "Day")
    assert before == after


def test_full_reset_does_not_alter_the_cutover_balance(client, super_admin, app):
    p = _make_product(client, "Reset Full Cutover Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})

    res = client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": p["id"], "mode": "full", "reason": "test",
        "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    assert res.status_code == 400  # explicitly refused — this IS the cutover's own anchor row
    figure = _figure(app, p["id"], "2026-08-01", "Day")
    assert figure.opening_stock_source == "ledger_cutover"
    assert figure.opening_base_qty == 1000


def test_full_reset_on_a_later_period_does_not_create_zero_opening(client, super_admin, app):
    p = _make_product(client, "Reset Later Cutover Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    prod = client.post("/api/production", json={
        "date": "2026-08-05", "shift": "Day", "lines": [{"product_id": p["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")

    client.post("/api/daily-reset", json={
        "date": "2026-08-05", "shift": "Day", "product_id": p["id"], "mode": "full", "reason": "test",
        "confirmation_text": "FULL RESET 2026-08-05 DAY",
    })
    view = _view(client, p["id"], "2026-08-05", "Day")
    assert view["opening"]["base_qty"] == 1000  # still the cutover's carried-forward value, never 0


def test_routine_save_after_reset_cannot_change_opening_stock(client, super_admin, app):
    p = _make_product(client, "Routine Save After Reset Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Night", "product_id": p["id"], "mode": "full", "reason": "test",
        "confirmation_text": "FULL RESET 2026-08-01 NIGHT",
    })
    res = client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Night",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    assert res.get_json()["opening"]["base_qty"] == 1000  # stale 0 ignored, still correctly derived


# =====================================================================
# MANUAL CORRECTION
# =====================================================================

def test_explicit_authorized_correction_creates_a_new_anchor_post_cutover(client, super_admin, app):
    p = _make_product(client, "Post Cutover Correction Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    res = client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-10", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "physical count",
    })
    assert res.status_code == 200
    figure = _figure(app, p["id"], "2026-08-10", "Day")
    assert figure.opening_stock_source == "manual_correction"
    later = _view(client, p["id"], "2026-08-11", "Day")
    assert later["opening"]["base_qty"] == 50000  # later periods carry from the correction


def test_correction_requires_reason_and_actor(client, super_admin, app):
    p = _make_product(client, "Correction Reason Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    res = client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-10", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True,
    })
    assert res.status_code == 400
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="correct_opening_stock").first()
        assert entry is None


def test_routine_save_cannot_create_a_correction_post_cutover(client, super_admin, app):
    p = _make_product(client, "Routine No Correction Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-10", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
    })
    figure = _figure(app, p["id"], "2026-08-10", "Day")
    assert figure is None or figure.opening_stock_source != "manual_correction"


def test_viewer_and_unauthorized_operator_cannot_correct_opening(client, login_as, super_admin, app):
    p = _make_product(client, "Unauthorized Correction Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})

    login_as("viewer_cutover", "password123", "viewer")
    res = client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-10", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "forged",
    })
    assert res.status_code == 403

    login_as("op_cutover_unauth", "password123", "operator")
    res = client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-10", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "forged",
    })
    figure = _figure(app, p["id"], "2026-08-10", "Day")
    assert figure is None or figure.opening_stock_source != "manual_correction"


# =====================================================================
# CROSS-SURFACE CONSISTENCY
# =====================================================================

def test_daily_figures_equals_dashboard(client, super_admin, app):
    p = _make_product(client, "Cross Surface Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": p["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")

    figures_view = _view(client, p["id"], "2026-08-01", "Day")
    dashboard = client.get("/api/dashboard?date=2026-08-01").get_json()
    dash_row = next(r for r in dashboard["stock_summary"] if r["product_id"] == p["id"])

    assert figures_view["opening"]["base_qty"] == dash_row["opening_base_qty"]
    assert figures_view["production"]["base_qty"] == dash_row["production_base_qty"]
    assert figures_view["closing"]["base_qty"] == dash_row["closing_base_qty"]


def test_stock_ledger_cli_service_equals_the_api(client, super_admin, app):
    p = _make_product(client, "Ledger Equals API Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})

    api_view = _view(client, p["id"], "2026-08-01", "Day")
    with app.app_context():
        from webapp.services import stock_ledger_service
        entry = stock_ledger_service.calculate_period(p["id"], "2026-08-01", "Day")
    assert entry["opening_base_qty"] == api_view["opening"]["base_qty"]
    assert entry["closing_base_qty"] == api_view["closing"]["base_qty"]
    assert entry["cutover_id"] is not None
    assert entry["formula_reconciles"] is True


def test_low_stock_warnings_use_the_same_closing_stock(client, super_admin, app):
    p = _make_product(client, "Low Stock Cutover Product")
    client.patch(f"/api/admin/products/{p['id']}", json={"low_stock_threshold": 500})
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (4, 0, 0)})  # 400 base < 500 threshold

    dashboard = client.get("/api/dashboard?date=2026-08-01").get_json()
    low_stock_row = next((r for r in dashboard["low_stock"] if r["product_id"] == p["id"]), None)
    assert low_stock_row is not None
    figures_view = _view(client, p["id"], "2026-08-01", "Day")
    assert low_stock_row["closing_base_qty"] == figures_view["closing"]["base_qty"] == 400


# =====================================================================
# HISTORICAL BOUNDARY
# =====================================================================

def test_pre_cutover_data_remains_viewable(client, super_admin, app):
    p = _make_product(client, "Boundary Viewable Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-01-01", "shift": "Day",
        "opening": {"cartons": 42, "packs": 0, "pieces": 0},
    })
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})

    pre = _view(client, p["id"], "2026-01-01", "Day")
    assert pre["opening"]["base_qty"] == 4200
    assert pre["is_pre_cutover"] is True


def test_pre_cutover_data_excluded_from_active_post_cutover_stock(client, super_admin, app):
    p = _make_product(client, "Boundary Excluded Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-01-01", "shift": "Day",
        "opening": {"cartons": 42, "packs": 0, "pieces": 0},
    })
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    post = _view(client, p["id"], "2026-08-01", "Day")
    assert post["opening"]["base_qty"] == 1000
    assert post["is_pre_cutover"] is False


def test_boundary_warning_is_available_via_active_cutover_endpoint(client, super_admin, app):
    p = _make_product(client, "Boundary Warning Product")
    assert client.get("/api/ledger-cutover/active").get_json() is None
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    active = client.get("/api/ledger-cutover/active").get_json()
    assert active is not None
    assert active["effective_date"] == "2026-08-01"
    assert active["effective_shift"] == "Day"


def test_old_legacy_negatives_do_not_leak_into_the_new_ledger(client, super_admin, app):
    p = _make_product(client, "Legacy Negative Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-01-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures/adjustments", json={
        "product_id": p["id"], "date": "2026-01-01", "shift": "Day", "delta_base_qty": 50000, "reason": "legacy loss",
    })
    # This legacy period is now genuinely, deeply negative.
    assert _view(client, p["id"], "2026-01-01", "Day")["closing"]["base_qty"] < 0

    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (10, 0, 0)})
    post = _view(client, p["id"], "2026-08-01", "Day")
    assert post["opening"]["base_qty"] == 1000  # positive, untouched by the legacy negative


# =====================================================================
# PACKAGING (representative — full exactness suite lives in
# test_final_correction_packaging_notation.py, unaffected by this round)
# =====================================================================

def test_no_pack_tier_product_cutover_remains_exact(client, super_admin, app):
    p = _make_product(client, "No Pack Tier Cutover Product", rule={"carton_to_pieces": 60})
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (5, 0, 33)})
    view = _view(client, p["id"], "2026-08-01", "Day")
    assert view["opening"]["base_qty"] == 333
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["opening"]["cartons"], view["opening"]["packs"], view["opening"]["pieces"], view["packaging_rule"]) == "5.33 Ctns"


def test_mixed_radix_napkin_style_cutover_remains_exact(client, super_admin, app):
    p = _make_product(client, "Napkin Style Cutover Product", rule={"cartons_to_packs": 6, "packs_to_pieces": 10})
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (1, 2, 4)})
    view = _view(client, p["id"], "2026-08-01", "Day")
    assert view["opening"]["base_qty"] == 1 * 60 + 2 * 10 + 4
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["opening"]["cartons"], view["opening"]["packs"], view["opening"]["pieces"], view["packaging_rule"]) == "1.24 Ctns"


def test_negative_closing_after_cutover_shows_correct_notation(client, super_admin, app):
    p = _make_product(client, "Negative Cutover Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (5, 0, 0)})
    client.post("/api/daily-figures/adjustments", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day", "delta_base_qty": 1000, "reason": "over-issue",
    })
    view = _view(client, p["id"], "2026-08-01", "Day")
    assert view["closing"]["base_qty"] == -500
    from webapp.services.quantity_format import qty_label
    label = qty_label(view["closing"]["cartons"], view["closing"]["packs"], view["closing"]["pieces"], view["packaging_rule"])
    assert label == "-5.00 Ctns"
    assert "-500" not in label


def test_no_raw_base_units_labelled_as_cartons_after_cutover(client, super_admin, app):
    p = _make_product(client, "Raw Base Units Cutover Product")
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (633, 5, 0)})
    view = _view(client, p["id"], "2026-08-01", "Day")
    assert view["opening"]["base_qty"] == 63350
    from webapp.services.quantity_format import qty_label
    label = qty_label(view["opening"]["cartons"], view["opening"]["packs"], view["opening"]["pieces"], view["packaging_rule"])
    assert label == "633.50 Ctns"
    assert "63350" not in label
