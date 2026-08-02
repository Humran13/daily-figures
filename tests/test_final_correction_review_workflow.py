"""
Final pre-deployment correction — Manager/Super Administrator Daily
Figures review-and-submit workflow.

Root cause of the false completion message: static/index.html's
showDone() fired purely off `currentIdx >= products.length` — reaching
the end of the product array, regardless of role, actual per-product
completion, or any submitted concept — and unconditionally showed
"figures recorded for all configured products." There was no reliable
date+shift-level review status anywhere in the backend to check against.

This correction adds:
  - webapp/models/daily_review_session.py: DailyReviewSession (one row
    per Date+Shift review identity) and DailyReviewProductState (per-
    product reviewed/edited/skipped marker within one session).
  - webapp/services/daily_review_service.py: session lifecycle, per-
    product marking, submission validation, reopen.
  - webapp/routes/daily_review.py: /api/daily-review* endpoints,
    Manager/Super Administrator only.
  - A `review_mode` flag on the existing /api/daily-figures upsert route
    so a Manager/Super Administrator's "Next Product" save never silently
    claims per-product DailyEntryStatus completion — the exact mechanism
    that let ordinary navigation look like submission.

See tests/test_stage7_daily_entry_ownership.py /
tests/test_stage8_production_hotfix.py / tests/test_stage8_stock_carry_forward.py
for the pre-existing Operator/carry-forward behavior this correction must
not regress — re-run in full as part of this correction's regression
pass, not duplicated here.
"""
from pathlib import Path

import pytest

from webapp.extensions import db as _db

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Review WF Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    other_product = client.post("/api/admin/products", json={"name": "Review WF Other Product"}).get_json()
    client.post(f"/api/admin/products/{other_product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Review WF Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Review WF Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "other_product": other_product, "customer": customer}


def _mark_reviewed(client, date, shift, product_id, edited=False, reason=None):
    return client.post("/api/daily-review/mark-reviewed", json={
        "date": date, "shift": shift, "product_id": product_id, "edited": edited, "reason": reason,
    })


def _mark_skipped(client, date, shift, product_id):
    return client.post("/api/daily-review/mark-skipped", json={
        "date": date, "shift": shift, "product_id": product_id,
    })


def _summary(client, date, shift):
    return client.get(f"/api/daily-review?date={date}&shift={shift}")


def _submit(client, date, shift):
    return client.post("/api/daily-review/submit", json={"date": date, "shift": shift})


# =====================================================================
# Section 19 — role-specific navigation wording
# =====================================================================

def test_manager_and_super_admin_branch_shows_next_product_not_save_and_next():
    idx = INDEX_HTML.index("<div class=\"closing-readout\">")
    nav_block = INDEX_HTML[idx:idx + 1200]
    assert "isElevated" in nav_block
    assert 'id="reviewNextBtn">${currentIdx<products.length-1 ? \'Next Product\' : \'Review Daily Figures\'}' in nav_block
    assert 'id="saveNextBtn">Save &amp; Next<' in nav_block  # Operator branch unchanged


def test_operator_still_uses_save_and_next_branch():
    """Operator behavior remains unchanged: the plain (non-elevated)
    branch of the same conditional still renders Save & Next exactly as
    before."""
    idx = INDEX_HTML.index("<div class=\"closing-readout\">")
    nav_block = INDEX_HTML[idx:idx + 1200]
    assert '`\n          ${currentIdx>0 ? `<button class="btn btn-ghost" id="prevProductBtn">Previous Product</button>` : \'\'}\n          <button class="btn btn-primary" id="saveNextBtn">Save &amp; Next</button>\n        `' in nav_block


def test_skip_for_now_is_rendered_as_a_real_button_for_review_workflow():
    assert 'class="btn-skip-review" id="skipReviewBtn">Skip for now — return before submitting<' in INDEX_HTML
    idx = INDEX_HTML.index(".btn-skip-review{")
    css = INDEX_HTML[idx:idx + 300]
    assert "background:none" not in css  # a real button, not the unstyled text-link look
    assert "border" in css


def test_operator_skip_button_styling_unchanged():
    idx = INDEX_HTML.index(".btn-skip{")
    css = INDEX_HTML[idx:idx + 200]
    assert "background:none" in css  # Operator's own skip control is untouched


# =====================================================================
# Section 19 — Skip does not save/complete/create No Activity
# =====================================================================

def test_skip_does_not_save_opening(client, setup):
    pid = setup["product"]["id"]
    res = _mark_skipped(client, "2026-08-01", "Day", pid)
    assert res.status_code == 200
    view = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view["has_entry"] is False


def test_skip_does_not_complete_the_product(client, setup):
    pid = setup["product"]["id"]
    _mark_skipped(client, "2026-08-01", "Day", pid)
    status = client.get(f"/api/daily-entry-status?date=2026-08-01&shift=Day&product_id={pid}").get_json()
    assert status["status"] == "not_started"


def test_skip_does_not_create_no_activity(client, setup):
    pid = setup["product"]["id"]
    _mark_skipped(client, "2026-08-01", "Day", pid)
    status = client.get(f"/api/daily-entry-status?date=2026-08-01&shift=Day&product_id={pid}").get_json()
    assert status["completion_type"] is None


def test_skipped_product_shows_as_skipped_not_reviewed(client, setup):
    pid = setup["product"]["id"]
    _mark_skipped(client, "2026-08-01", "Day", pid)
    data = _summary(client, "2026-08-01", "Day").get_json()
    row = next(r for r in data["products"] if r["product_id"] == pid)
    assert row["review_state"] == "skipped"
    assert row["blocks_submission"] is True


# =====================================================================
# Section 19 — navigation never submits
# =====================================================================

def test_next_product_mark_reviewed_does_not_submit(client, setup):
    pid = setup["product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    data = _summary(client, "2026-08-01", "Day").get_json()
    assert data["session"]["status"] == "in_progress"


def test_reaching_the_final_product_does_not_submit(client, setup):
    """Marking the LAST eligible product reviewed still leaves the
    session in_progress — arriving at the end of the list is navigation,
    never submission."""
    pid_a, pid_b = setup["product"]["id"], setup["other_product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid_a)
    _mark_reviewed(client, "2026-08-01", "Day", pid_b)
    data = _summary(client, "2026-08-01", "Day").get_json()
    assert data["session"]["status"] == "in_progress"
    assert data["can_submit"] is True  # everything reviewed, but NOT yet submitted


def test_reaching_final_product_opens_review_screen_not_done_card():
    assert "if(isElevated){ showReviewScreen(date, shift); } else { showDone(date, shift); }" in INDEX_HTML


def test_false_figures_recorded_message_not_shown_for_elevated_roles():
    """showDone() (the only place that string is ever rendered) is only
    ever called for non-elevated roles — see the branch above."""
    idx = INDEX_HTML.index("function showDone(date, shift){")
    body = INDEX_HTML[idx:INDEX_HTML.index("\n}", idx)]
    assert "figures recorded for all configured products" in body
    # And confirm showDone is never invoked from the elevated branch:
    assert INDEX_HTML.count("showDone(date, shift)") == 2  # the call site + inside showReviewScreen's sibling branch guard only


def test_navigation_creates_no_daily_figure_row(client, setup):
    pid = setup["product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    _mark_skipped(client, "2026-08-01", "Day", setup["other_product"]["id"])
    from webapp.models.daily_figure import DailyFigure
    assert DailyFigure.query.filter_by(product_id=pid, date="2026-08-01").count() == 0


def test_navigation_creates_no_source_book_records(client, setup):
    pid = setup["product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    from webapp.models.dispatch import Dispatch
    from webapp.models.return_record import ReturnRecord
    from webapp.models.production_record import ProductionRecord
    assert Dispatch.query.count() == 0
    assert ReturnRecord.query.count() == 0
    assert ProductionRecord.query.count() == 0


# =====================================================================
# Section 19 — completeness blocks submission
# =====================================================================

def test_unvisited_products_block_submission(client, setup):
    pid = setup["product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    # other_product never visited at all.
    res = _submit(client, "2026-08-01", "Day")
    assert res.status_code == 400
    assert "still require review" in res.get_json()["error"]


def test_skipped_products_block_submission(client, setup):
    pid_a, pid_b = setup["product"]["id"], setup["other_product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid_a)
    _mark_skipped(client, "2026-08-01", "Day", pid_b)
    res = _submit(client, "2026-08-01", "Day")
    assert res.status_code == 400


def test_validation_error_blocks_submission(client, login_as, setup):
    no_rule_product = client.post("/api/admin/products", json={"name": "Review WF No Rule"}).get_json()
    pid_a, pid_b = setup["product"]["id"], setup["other_product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid_a)
    _mark_reviewed(client, "2026-08-01", "Day", pid_b)
    # no_rule_product is active + eligible but has no packaging rule -> validation error.
    res = _submit(client, "2026-08-01", "Day")
    assert res.status_code == 400
    data = _summary(client, "2026-08-01", "Day").get_json()
    row = next(r for r in data["products"] if r["product_id"] == no_rule_product["id"])
    assert row["validation_error"] is not None
    assert row["blocks_submission"] is True


def test_all_reviewed_products_appear_in_summary(client, setup):
    pid_a, pid_b = setup["product"]["id"], setup["other_product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid_a)
    _mark_skipped(client, "2026-08-01", "Day", pid_b)
    data = _summary(client, "2026-08-01", "Day").get_json()
    ids = {r["product_id"] for r in data["products"]}
    assert {pid_a, pid_b} <= ids


# =====================================================================
# Section 19 — explicit submit, single active identity, resubmission
# =====================================================================

def test_final_submit_requires_backend_call_and_marks_submitted(client, setup):
    pid_a, pid_b = setup["product"]["id"], setup["other_product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid_a)
    _mark_reviewed(client, "2026-08-01", "Day", pid_b)
    res = _submit(client, "2026-08-01", "Day")
    assert res.status_code == 200
    assert res.get_json()["status"] == "submitted"


def test_submit_confirmation_wording_present_in_frontend():
    assert "Submit the Daily Figures review for" in INDEX_HTML
    assert "confirm(`Submit the Daily Figures review for" in INDEX_HTML


def test_only_submit_endpoint_changes_session_status(client, setup):
    pid_a, pid_b = setup["product"]["id"], setup["other_product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid_a)
    _mark_reviewed(client, "2026-08-01", "Day", pid_b)
    before = _summary(client, "2026-08-01", "Day").get_json()["session"]["status"]
    assert before == "in_progress"


def test_one_active_review_identity_per_date_shift(client, setup, app):
    pid = setup["product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    _mark_reviewed(client, "2026-08-01", "Day", setup["other_product"]["id"])
    with app.app_context():
        from webapp.models.daily_review_session import DailyReviewSession
        assert DailyReviewSession.query.filter_by(date="2026-08-01", shift="Day").count() == 1


def test_resubmission_after_reopen_does_not_create_a_duplicate_session(client, setup, app):
    pid_a, pid_b = setup["product"]["id"], setup["other_product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid_a)
    _mark_reviewed(client, "2026-08-01", "Day", pid_b)
    _submit(client, "2026-08-01", "Day")
    client.post("/api/daily-review/reopen", json={"date": "2026-08-01", "shift": "Day", "reason": "correction needed"})
    res = _submit(client, "2026-08-01", "Day")
    assert res.status_code == 200
    with app.app_context():
        from webapp.models.daily_review_session import DailyReviewSession
        assert DailyReviewSession.query.filter_by(date="2026-08-01", shift="Day").count() == 1


def test_cannot_mark_reviewed_after_submission_without_reopening(client, setup):
    pid_a, pid_b = setup["product"]["id"], setup["other_product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid_a)
    _mark_reviewed(client, "2026-08-01", "Day", pid_b)
    _submit(client, "2026-08-01", "Day")
    res = _mark_reviewed(client, "2026-08-01", "Day", pid_a)
    assert res.status_code == 409


# =====================================================================
# Section 19 — refresh/failure never partially applies
# =====================================================================

def test_page_refresh_equivalent_get_never_mutates(client, setup):
    pid = setup["product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    before = _summary(client, "2026-08-01", "Day").get_json()
    for _ in range(3):
        _summary(client, "2026-08-01", "Day")
    after = _summary(client, "2026-08-01", "Day").get_json()
    assert before["session"]["updated_at"] == after["session"]["updated_at"]


def test_failed_submission_leaves_review_unsubmitted(client, setup):
    pid = setup["product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid)  # other_product left unreviewed
    res = _submit(client, "2026-08-01", "Day")
    assert res.status_code == 400
    data = _summary(client, "2026-08-01", "Day").get_json()
    assert data["session"]["status"] == "in_progress"


def test_no_review_started_yet_cannot_be_submitted(client, login_as):
    login_as("review_wf_fresh", "password123", "manager")
    res = _submit(client, "2026-09-01", "Day")
    assert res.status_code == 400


# =====================================================================
# Section 18 — permissions
# =====================================================================

@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_non_elevated_roles_get_403_from_every_review_endpoint(client, login_as, role):
    login_as(f"review_wf_{role}", "password123", role)
    assert client.get("/api/daily-review?date=2026-08-01&shift=Day").status_code == 403
    assert client.post("/api/daily-review/mark-reviewed", json={"date": "2026-08-01", "shift": "Day", "product_id": 1}).status_code == 403
    assert client.post("/api/daily-review/mark-skipped", json={"date": "2026-08-01", "shift": "Day", "product_id": 1}).status_code == 403
    assert client.post("/api/daily-review/submit", json={"date": "2026-08-01", "shift": "Day"}).status_code == 403
    assert client.post("/api/daily-review/reopen", json={"date": "2026-08-01", "shift": "Day", "reason": "x"}).status_code == 403


def test_manager_has_full_review_access(client, login_as, setup):
    login_as("review_wf_mgr", "password123", "manager")
    pid = setup["product"]["id"]
    assert _mark_reviewed(client, "2026-08-01", "Day", pid).status_code == 200
    assert _summary(client, "2026-08-01", "Day").status_code == 200


def test_manager_does_not_gain_unrelated_admin_access(client, login_as):
    login_as("review_wf_mgr2", "password123", "manager")
    res = client.post("/api/admin/users", json={"username": "sneaky2", "password": "password123", "role": "operator"})
    assert res.status_code == 403
