"""
Minimal Daily Figures UI restoration: static/index.html's entry card was
put back to its earlier, more compact layout (single-row stock-readouts
with an inline hint next to each label, Opening Stock gated through the
same disabled-qtyInputsHtml pattern every other role-gated field already
uses) after a later correction had restructured it into a taller
"read-only card" design. The one quantity change that correction made —
Closing Stock displayed in book notation instead of raw pieces — is kept.

Source-level checks follow this project's established convention (no JS/
browser test runner exists here); functional checks go through the Flask
test client.
"""
import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
QUANTITY_FORMAT_JS = (STATIC_DIR / "quantity_format.js").read_text(encoding="utf-8")


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin):
    product = client.post("/api/admin/products", json={"name": "Restoration Test Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return {"product": product}


# ================= earlier layout markers restored =================

def test_entry_card_shows_date_shift_progress_and_product_name():
    assert '<input type="date" id="dateInput">' in INDEX_HTML
    assert 'id="shiftInput"' in INDEX_HTML
    assert 'id="progressFill"' in INDEX_HTML and 'id="progressLabel"' in INDEX_HTML
    assert 'class="product-name">${product.name}</p>' in INDEX_HTML


def test_stock_readout_is_single_row_with_inline_hint_not_a_sub_line():
    """The earlier, more compact layout keeps each field's hint inline
    inside the label span — no separate stock-readout-sub row beneath it.
    Stage 7 shortened the long "(auto — from the finalized X Book)"
    sentences down to a plain "(auto)" tag (unnecessary helper text removed,
    the labels themselves are unchanged) — see
    tests/test_stage7_daily_entry_ownership.py."""
    assert "stock-readout-sub" not in INDEX_HTML
    assert '<span class="lbl">Returns <span class="hint" style="font-weight:400;text-transform:none;">(auto)</span></span>' in INDEX_HTML
    assert '<span class="lbl">Production <span class="hint" style="font-weight:400;text-transform:none;">(auto)</span></span>' in INDEX_HTML
    assert '<span class="lbl">Issued <span class="hint" style="font-weight:400;text-transform:none;">(auto)</span></span>' in INDEX_HTML


def test_stock_readout_css_is_the_earlier_compact_single_line_rule():
    css_block = INDEX_HTML.split("<script>")[0]
    assert ".stock-readout{ display:flex; justify-content:space-between; align-items:center; background:var(--paper-dim); border-radius:10px; padding:12px 14px; margin-bottom:18px; }" in css_block
    assert "flex-wrap:wrap" not in css_block.split(".stock-readout{")[1][:5]  # not reintroduced onto .stock-readout itself


def test_opening_stock_section_present():
    assert 'id="openingReadout"' in INDEX_HTML
    assert "<span class=\"lbl\">Opening Stock</span>" in INDEX_HTML


def test_issued_row_has_view_link():
    assert 'id="issuedBreakdownBtn">view</button>' in INDEX_HTML


def test_closing_stock_is_the_dark_panel():
    assert '<div class="closing-readout"><span class="lbl">Closing Stock</span><span class="val" id="closingPreview">—</span></div>' in INDEX_HTML


def test_formula_hint_removed_stage7():
    # Stage 7 explicitly removed this explanatory sentence — the formula
    # itself (updatePreview()'s closingBase calculation) is unchanged.
    assert "Closing Stock = Opening Stock + Production + Returns &minus; Issued" not in INDEX_HTML


# ================= Closing Stock: book notation, never raw pieces, never em-dash =================

def test_closing_preview_never_renders_raw_pieces_suffix():
    idx = INDEX_HTML.index("function updatePreview(){")
    body = INDEX_HTML[idx:INDEX_HTML.index("\n  }", idx)]
    assert "' pieces'" not in body
    assert "qtyLabel(fromBaseUnitsPreview(closingBase, rule), rule)" in body


def test_qty_label_formats_zero_as_ctns_never_em_dash():
    """A valid zero must display as '0 Ctns', never '—' — qtyLabel only
    ever returns '—' when there's no part object at all (nothing computed
    yet), never for an actual zero quantity. qtyLabel now lives in the
    shared static/quantity_format.js (final pre-deployment correction —
    one centralized formatter, no longer duplicated per page)."""
    js = QUANTITY_FORMAT_JS
    idx = js.index("function qtyLabel(part, rule){")
    body = js[idx:js.index("\n}", idx)]
    assert "if(!part) return '—';" in body
    assert "part.packs === 0 && part.pieces === 0" in body
    assert "return `${part.cartons} Ctns`;" in body


def test_book_notation_examples_match_spec():
    js = QUANTITY_FORMAT_JS
    idx = js.index("function qtyLabel(part, rule){")
    body = js[idx:js.index("\n}", idx)]
    # 0 Ctns / 5 Ctns (packs=0,pieces=0 branch) and positional two-digit
    # notation (6.11 Ctns style) both come from this one function body.
    assert "`${part.cartons} Ctns`" in body
    assert "`${part.cartons}.${part.packs}${part.pieces} Ctns`" in body


def test_closing_stock_computed_values_render_in_book_notation(client, setup):
    """0 Ctns, 5 Ctns, and 6.11 Ctns end to end: the server-side formatter
    (webapp/services/quantity_format.qty_label, which the frontend mirrors)
    for the exact numbers used in the correction's own example."""
    from webapp.services.quantity_format import qty_label
    rule = {"cartons_to_packs": 10, "packs_to_pieces": 10, "carton_to_pieces": None}
    assert qty_label(0, 0, 0, rule) == "0 Ctns"
    assert qty_label(5, 0, 0, rule) == "5 Ctns"
    assert qty_label(6, 1, 1, rule) == "6.11 Ctns"


def test_no_pack_tier_product_uses_book_style_point_notation():
    """Final pre-deployment correction: superseded the older "Xc Ypc" form
    with the same point notation pack-tier products use — see
    tests/test_final_correction_packaging_notation.py."""
    kingmax_rule = {"cartons_to_packs": None, "packs_to_pieces": None, "carton_to_pieces": 60}
    from webapp.services.quantity_format import qty_label
    assert qty_label(2, 0, 5, kingmax_rule) == "2.05 Ctns"


# ================= Returns/Production remain source-derived and read-only =================

def test_returns_and_production_have_no_input_fields_in_the_entry_card():
    assert "qtyInputsHtml('ret'" not in INDEX_HTML
    assert "qtyInputsHtml('prod'" not in INDEX_HTML


def test_returns_and_production_view_fields_derive_from_stock_service(client, setup):
    pid = setup["product"]["id"]
    customer_id = client.post("/api/admin/customers", json={
        "name": f"Auto Returner {id(object())}", "confirm_not_duplicate": True,
    }).get_json()["id"]
    ret = client.post("/api/returns", json={
        "date": "2026-07-30", "customer_id": customer_id,
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{ret['id']}/finalize")
    prod = client.post("/api/production", json={
        "date": "2026-07-30", "shift": "Day",
        "lines": [{"product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")

    view = client.get(f"/api/daily-figures/{pid}?date=2026-07-30&shift=Day").get_json()
    assert view["return_"]["base_qty"] == 100
    assert view["production"]["base_qty"] == 200


# ================= authorized Opening Stock editing remains available =================

def test_opening_stock_input_gated_by_can_edit_opening_disabled_attribute():
    assert "qtyInputsHtml('opening', rule, view.opening, !canEditOpening)" in INDEX_HTML


def test_manager_can_still_save_opening_stock(client, setup):
    res = client.post("/api/daily-figures", json={
        "product_id": setup["product"]["id"], "date": "2026-07-30", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    assert res.get_json()["opening"]["base_qty"] == 500


def test_operator_without_permission_cannot_save_first_time_opening(client, setup, login_as):
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = client.post("/api/daily-figures", json={
        "product_id": setup["product"]["id"], "date": "2026-07-30", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 403


def test_viewer_cannot_save_opening_stock(client, setup, login_as):
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    res = client.post("/api/daily-figures", json={
        "product_id": setup["product"]["id"], "date": "2026-07-30", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 403


# ================= Save & Next / Skip only where authorized =================

def test_save_and_next_and_skip_conditioned_on_is_fully_read_only():
    """Final pre-deployment correction: the Skip button now branches a
    second time on isElevated (a real .btn-skip-review button with
    different wording for Manager/Super Administrator — see
    tests/test_final_correction_review_workflow.py — vs the unchanged
    .btn-skip text-link for Operator), so the exact old single-branch
    literal no longer appears verbatim; both branches are still present
    and reachable from the same isFullyReadOnly ? '' : (...) guard."""
    assert "${isFullyReadOnly ? '' : (isElevated" in INDEX_HTML
    assert '<button class="btn-skip" id="skipBtn">Skip for now' in INDEX_HTML
    # Relabeled "Skip to Submit" by the final UX/reporting package (jumps
    # straight to the Submit/Review screen) — see
    # tests/test_final_correction_review_workflow.py for that behavior.
    assert '<button class="btn-skip-review" id="skipReviewBtn">Skip to Submit<' in INDEX_HTML
    nav_row = re.search(r'<div class="nav-row">\s*\$\{isFullyReadOnly.*?</div>', INDEX_HTML, re.DOTALL).group(0)
    assert 'id="saveNextBtn">Save &amp; Next<' in nav_row
    assert 'id="nextProductBtn">Next Product<' in nav_row


# ================= Previous/Next Product navigation still works =================

def test_next_and_previous_product_handlers_unchanged():
    assert "const nextProductBtn = document.getElementById('nextProductBtn');" in INDEX_HTML
    assert "const prevProductBtn = document.getElementById('prevProductBtn');" in INDEX_HTML
    next_body = re.search(
        r"if\(nextProductBtn\) nextProductBtn\.addEventListener\('click', \(\)=>\{([^}]*)\}\);", INDEX_HTML,
    ).group(1)
    # Stage 7 added a fire-and-forget lock release before advancing.
    assert next_body.strip() == "releaseLockIfOwned(product, date, shift); currentIdx++; renderEntryCard();"


def test_date_and_shift_read_fresh_on_every_render():
    assert "const date = document.getElementById('dateInput').value;" in INDEX_HTML
    assert "const shift = document.getElementById('shiftInput').value;" in INDEX_HTML


# ================= existing calculations/permissions unchanged =================

def test_closing_stock_formula_value_matches_opening_plus_production_plus_returns_minus_issued(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-30", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    customer_id = client.post("/api/admin/customers", json={
        "name": f"Auto Returner {id(object())}", "confirm_not_duplicate": True,
    }).get_json()["id"]
    ret = client.post("/api/returns", json={
        "date": "2026-07-30", "customer_id": customer_id,
        "lines": [{"product_id": pid, "cartons": 0, "packs": 5, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{ret['id']}/finalize")
    prod = client.post("/api/production", json={
        "date": "2026-07-30", "shift": "Day",
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")

    view = client.get(f"/api/daily-figures/{pid}?date=2026-07-30&shift=Day").get_json()
    assert view["closing"]["base_qty"] == 1000 + 50 + 100 - 0


def test_branding_and_role_based_nav_untouched():
    assert "async function applyBranding()" in INDEX_HTML
    assert "function setRoleVisible(el, visible){" in INDEX_HTML
    # Stage 6 replaced the old operatorNav visibility toggle with the
    # shared, role-aware nav rendered by static/app-shell.js — see
    # tests/test_stage6_app_shell.py for that architecture's coverage.
    assert 'id="operatorNav"' not in INDEX_HTML
    assert 'id="appRoleNav"' in INDEX_HTML
