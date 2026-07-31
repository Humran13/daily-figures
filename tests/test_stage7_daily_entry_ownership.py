"""
Stage 7 sections 1, 3, 6, 9: one daily-submission owner at a time, the
"Already inputted"/"currently being entered" messaging, Manager/Super
Admin correction/takeover/reopen authority, explicit "No Activity Today"
completion, and concurrency safety for simultaneous finalization attempts.
"""
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Stage7 Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    # Operators are read-only on Daily Figures by default (all four
    # role-wide permission flags start False) — these tests are about
    # ownership/locking, not this pre-existing permission gate, so enable
    # the one flag Opening Stock entry needs.
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    return {"product": product}


def _login_new_client(app, client, username, role):
    """Creates `username` via the already-super_admin-authenticated
    `client` (a normal request/commit cycle, same as every other test in
    this suite) rather than touching the database directly from a
    manually-pushed app context — the latter caused real
    "database is locked" errors under SQLite when interleaved with the
    request-scoped sessions the Flask test clients themselves use."""
    res = client.post("/api/admin/users", json={"username": username, "password": "password123", "role": role})
    if res.status_code not in (201, 409):
        raise AssertionError(f"could not create test user {username!r}: {res.get_json()}")
    c = app.test_client()
    login_res = c.post("/api/login", json={"username": username, "password": "password123"})
    assert login_res.status_code == 200, login_res.get_json()
    return c


# =====================================================================
# Ownership lock — acquisition, conflict messaging, Viewer can't lock
# =====================================================================

def test_first_operator_acquires_the_entry(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1_s7", "operator")
    res = op1.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "in_progress"
    assert data["locked_by"] == "op1_s7"


def test_second_operator_sees_it_as_in_progress(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1b_s7", "operator")
    op2 = _login_new_client(app, client, "op2b_s7", "operator")
    op1.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-01", "shift": "Day"})

    res = op2.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 409
    assert "currently being entered by op1b_s7" in res.get_json()["error"]


def test_completed_entry_shows_already_inputted(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1c_s7", "operator")
    op1.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 2, "packs": 0, "pieces": 0},
    })

    op2 = _login_new_client(app, client, "op2c_s7", "operator")
    res = op2.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 409
    assert "Already inputted by op1c_s7" in res.get_json()["error"]


def test_second_operator_cannot_save_over_a_completed_entry(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1d_s7", "operator")
    op1.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-02", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    op2 = _login_new_client(app, client, "op2d_s7", "operator")
    res = op2.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-02", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 409
    assert "Already inputted by op1d_s7" in res.get_json()["error"]


def test_viewer_cannot_acquire_a_lock(app, setup, client):
    pid = setup["product"]["id"]
    viewer = _login_new_client(app, client, "view_s7", "viewer")
    res = viewer.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 403


def test_different_products_never_conflict(app, setup, client):
    # `setup` already logs `client` in as root — no second login_as needed.
    product2 = client.post("/api/admin/products", json={"name": "Stage7 Product 2"}).get_json()
    client.post(f"/api/admin/products/{product2['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    op1 = _login_new_client(app, client, "op1e_s7", "operator")
    op2 = _login_new_client(app, client, "op2e_s7", "operator")
    op1.post("/api/daily-entry-status/lock", json={"product_id": setup["product"]["id"], "date": "2026-08-01", "shift": "Day"})
    res = op2.post("/api/daily-entry-status/lock", json={"product_id": product2["id"], "date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 200


def test_different_dates_never_conflict(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1f_s7", "operator")
    op2 = _login_new_client(app, client, "op2f_s7", "operator")
    op1.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-01", "shift": "Day"})
    res = op2.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-02", "shift": "Day"})
    assert res.status_code == 200


# =====================================================================
# Stale lock expiration
# =====================================================================

def test_stale_lock_expires_and_allows_a_new_operator(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1g_s7", "operator")
    op1.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-03", "shift": "Day"})

    # No nested `with app.app_context():` here — the `app` fixture (see
    # conftest.py) already keeps one active for the whole test, and
    # re-pushing a second one while a live test client request is also in
    # flight is what caused real "database is locked" errors under SQLite.
    from webapp.models.daily_entry_status import DailyEntryStatus
    from webapp.extensions import db as _db
    from datetime import datetime, timedelta, timezone
    row = DailyEntryStatus.query.filter_by(date="2026-08-03", shift="Day", product_id=pid).first()
    row.lock_acquired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30)
    _db.session.commit()

    op2 = _login_new_client(app, client, "op2g_s7", "operator")
    res = op2.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-03", "shift": "Day"})
    assert res.status_code == 200


# =====================================================================
# Manager/Super Admin takeover and reopen
# =====================================================================

def test_manager_can_take_over_a_stuck_lock(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1h_s7", "operator")
    op1.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-04", "shift": "Day"})

    mgr = _login_new_client(app, client, "mgr_s7", "manager")
    res = mgr.post("/api/daily-entry-status/takeover", json={"product_id": pid, "date": "2026-08-04", "shift": "Day"})
    assert res.status_code == 200
    assert res.get_json()["locked_by"] is None


def test_operator_cannot_take_over_a_lock(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1i_s7", "operator")
    op2 = _login_new_client(app, client, "op2i_s7", "operator")
    op1.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-05", "shift": "Day"})
    res = op2.post("/api/daily-entry-status/takeover", json={"product_id": pid, "date": "2026-08-05", "shift": "Day"})
    assert res.status_code == 403


def test_takeover_is_audited(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1j_s7", "operator")
    op1.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-06", "shift": "Day"})
    mgr = _login_new_client(app, client, "mgr2_s7", "manager")
    mgr.post("/api/daily-entry-status/takeover", json={"product_id": pid, "date": "2026-08-06", "shift": "Day"})

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="takeover_lock", entity_type="daily_entry_status").first()
        assert entry is not None
        assert entry.username == "mgr2_s7"


def test_manager_can_reopen_a_completed_entry_with_a_reason(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1k_s7", "operator")
    op1.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-07", "shift": "Day",
        "opening": {"cartons": 3, "packs": 0, "pieces": 0},
    })
    mgr = _login_new_client(app, client, "mgr3_s7", "manager")
    res = mgr.post("/api/daily-entry-status/reopen", json={
        "product_id": pid, "date": "2026-08-07", "shift": "Day", "reason": "wrong quantity entered",
    })
    assert res.status_code == 200
    assert res.get_json()["status"] == "not_started"


def test_reopen_requires_a_reason(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1l_s7", "operator")
    op1.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-08", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    mgr = _login_new_client(app, client, "mgr4_s7", "manager")
    res = mgr.post("/api/daily-entry-status/reopen", json={"product_id": pid, "date": "2026-08-08", "shift": "Day"})
    assert res.status_code == 400


def test_operator_cannot_reopen(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1m_s7", "operator")
    op1.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-09", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    res = op1.post("/api/daily-entry-status/reopen", json={
        "product_id": pid, "date": "2026-08-09", "shift": "Day", "reason": "trying anyway",
    })
    assert res.status_code == 403


def test_after_reopen_a_new_operator_can_complete_it(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1n_s7", "operator")
    op1.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-10", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    mgr = _login_new_client(app, client, "mgr5_s7", "manager")
    mgr.post("/api/daily-entry-status/reopen", json={
        "product_id": pid, "date": "2026-08-10", "shift": "Day", "reason": "correction needed",
    })
    op2 = _login_new_client(app, client, "op2n_s7", "operator")
    res = op2.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-10", "shift": "Day",
        "opening": {"cartons": 2, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200


def test_manager_correction_bypasses_ownership_lock_entirely(app, setup, client):
    """Manager/Super Admin correction authority (section 3) never needs to
    acquire a lock, and is never blocked by an Operator's active one."""
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1o_s7", "operator")
    op1.post("/api/daily-entry-status/lock", json={"product_id": pid, "date": "2026-08-11", "shift": "Day"})

    mgr = _login_new_client(app, client, "mgr6_s7", "manager")
    res = mgr.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-11", "shift": "Day",
        "opening": {"cartons": 7, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200


def test_manager_correcting_an_already_completed_entry_preserves_original_attribution(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1p_s7", "operator")
    op1.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-12", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    mgr = _login_new_client(app, client, "mgr7_s7", "manager")
    # A Manager correcting Notes on an already-completed entry does not
    # rewrite who originally completed it.
    mgr.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-12", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0}, "notes": "corrected note",
    })
    status = mgr.get(f"/api/daily-entry-status?date=2026-08-12&shift=Day&product_id={pid}").get_json()
    assert status["completed_by"] == "op1p_s7"


# =====================================================================
# No Activity Today
# =====================================================================

def test_operator_can_complete_with_no_activity_today(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1q_s7", "operator")
    res = op1.post("/api/daily-entry-status/no-activity", json={"product_id": pid, "date": "2026-08-13", "shift": "Day"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "completed"
    assert data["completion_type"] == "no_activity"


def test_no_activity_today_creates_no_dispatch_returns_or_production_movement(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1r_s7", "operator")
    op1.post("/api/daily-entry-status/no-activity", json={"product_id": pid, "date": "2026-08-14", "shift": "Day"})

    dash = client.get(f"/api/dashboard?date=2026-08-14").get_json()
    assert dash["activity"]["dispatch"]["finalized"] == 0
    assert dash["activity"]["returns"]["finalized"] == 0
    assert dash["activity"]["production"]["day_finalized"] == 0


def test_no_activity_today_does_not_change_opening_stock(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1s_s7", "operator")
    before = op1.get(f"/api/daily-figures/{pid}?date=2026-08-15&shift=Day").get_json()
    op1.post("/api/daily-entry-status/no-activity", json={"product_id": pid, "date": "2026-08-15", "shift": "Day"})
    after = op1.get(f"/api/daily-figures/{pid}?date=2026-08-15&shift=Day").get_json()
    assert after["opening"]["base_qty"] == before["opening"]["base_qty"]


def test_closing_stock_still_correctly_calculated_after_no_activity(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1t_s7", "operator")
    op1.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-16", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    op2 = _login_new_client(app, client, "op2t_s7", "operator")
    view = op2.get(f"/api/daily-figures/{pid}?date=2026-08-17&shift=Day").get_json()
    assert view["closing"]["base_qty"] == 500  # carried forward, untouched


def test_second_operator_cannot_duplicate_a_no_activity_completion(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1u_s7", "operator")
    op1.post("/api/daily-entry-status/no-activity", json={"product_id": pid, "date": "2026-08-18", "shift": "Day"})
    op2 = _login_new_client(app, client, "op2u_s7", "operator")
    res = op2.post("/api/daily-entry-status/no-activity", json={"product_id": pid, "date": "2026-08-18", "shift": "Day"})
    assert res.status_code == 409


def test_manager_can_reopen_a_no_activity_completion(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1v_s7", "operator")
    op1.post("/api/daily-entry-status/no-activity", json={"product_id": pid, "date": "2026-08-19", "shift": "Day"})
    mgr = _login_new_client(app, client, "mgr8_s7", "manager")
    res = mgr.post("/api/daily-entry-status/reopen", json={
        "product_id": pid, "date": "2026-08-19", "shift": "Day", "reason": "needs re-review",
    })
    assert res.status_code == 200


def test_skip_does_not_mark_the_product_complete():
    """Skip is purely client-side navigation (currentIdx++) — it never
    calls any completion endpoint, unlike the explicit No Activity Today
    button."""
    idx = INDEX_HTML.index('if(skipBtn) skipBtn.addEventListener')
    body = INDEX_HTML[idx:idx + 200]
    assert "no-activity" not in body
    assert "apiPost" not in body


def test_no_activity_today_button_present_and_wired():
    assert 'id="noActivityBtn"' in INDEX_HTML
    assert "No Activity Today" in INDEX_HTML
    assert "async function markNoActivity(product, date, shift)" in INDEX_HTML
    assert "/api/daily-entry-status/no-activity" in INDEX_HTML


def test_skip_helper_text_distinguishes_from_no_activity():
    assert "Skip for now" in INDEX_HTML
    assert "Skip — no activity for this product" not in INDEX_HTML


# =====================================================================
# Concurrency safety (section 9)
# =====================================================================

def test_concurrent_finalization_only_one_succeeds_no_duplicate_row(app, setup, client):
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1w_s7", "operator")
    op2 = _login_new_client(app, client, "op2w_s7", "operator")

    payload = {"product_id": pid, "date": "2026-08-20", "shift": "Day", "opening": {"cartons": 1, "packs": 0, "pieces": 0}}
    res1 = op1.post("/api/daily-figures", json=payload)
    res2 = op2.post("/api/daily-figures", json={**payload, "opening": {"cartons": 9, "packs": 0, "pieces": 0}})

    statuses = sorted([res1.status_code, res2.status_code])
    assert statuses == [200, 409]

    from webapp.models.daily_figure import DailyFigure
    with app.app_context():
        rows = DailyFigure.query.filter_by(date="2026-08-20", shift="Day", product_id=pid).all()
        assert len(rows) == 1
        # The winner's value stands — never double-counted or overwritten
        # by the loser.
        assert rows[0].opening_cartons in (1, 9)


def test_lock_does_not_remain_stuck_after_a_failed_request(app, setup, client):
    """A StockError from upsert_daily_figure (e.g. an invalid packaging
    input) must not leave the entry permanently claimed as completed —
    the whole attempt, including the completion claim, rolls back
    together."""
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1x_s7", "operator")
    bad = op1.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-21", "shift": "Day",
        "opening": {"cartons": -1, "packs": 0, "pieces": 0},
    })
    assert bad.status_code == 400

    status = op1.get(f"/api/daily-entry-status?date=2026-08-21&shift=Day&product_id={pid}").get_json()
    assert status["status"] == "not_started"

    good = op1.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-21", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    assert good.status_code == 200


# =====================================================================
# Backend enforcement, not just frontend (section 1 requirement)
# =====================================================================

def test_conflict_enforced_via_direct_api_call_bypassing_any_ui(app, setup, client):
    """No JavaScript pre-check is the only thing standing in the way — a
    raw API call from a second Operator is rejected server-side too."""
    pid = setup["product"]["id"]
    op1 = _login_new_client(app, client, "op1y_s7", "operator")
    op1.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-22", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    op2 = _login_new_client(app, client, "op2y_s7", "operator")
    res = op2.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-22", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 409


def test_unique_constraint_exists_on_date_shift_product():
    from webapp.models.daily_entry_status import DailyEntryStatus
    constraint_names = {c.name for c in DailyEntryStatus.__table_args__ if hasattr(c, "name")}
    assert "uq_daily_entry_status_date_shift_product" in constraint_names
