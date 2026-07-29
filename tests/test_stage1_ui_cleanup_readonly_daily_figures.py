"""
Stage 1 UI cleanup: Operator/Viewer previously saw both the old internal
Enter/History & Export tabs AND the new 3-item operatorNav simultaneously
(duplicate navigation), and a fully-locked Operator/Viewer still saw an
enabled-looking "Save & Next"/"Skip" pair with only a vague hint. This is a
UI-only correction — no calculation or backend-permission logic changes,
so these are all source-level regression guards (same approach as every
prior frontend-only stage in this project, which has no JS/browser test
runner).
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ---------- no duplicate navigation ----------

def test_primary_tabs_and_operator_nav_are_mutually_exclusive_by_role():
    assert "document.getElementById('primaryTabs').classList.toggle('hidden', isOperatorOrViewer);" in INDEX_HTML
    assert "document.getElementById('operatorNav').classList.toggle('hidden', !isOperatorOrViewer);" in INDEX_HTML


def test_old_internal_tabs_still_exist_for_manager_and_super_admin():
    """Requirement: Manager/Super Admin's interface is unchanged — the old
    tabs must still exist in markup, just hidden for Operator/Viewer."""
    assert '<div class="tab active" data-tab="entry">Enter</div>' in INDEX_HTML
    assert '<div class="tab" data-tab="history">History &amp; Export</div>' in INDEX_HTML
    assert 'id="primaryTabs"' in INDEX_HTML


def test_primary_tabs_container_wraps_both_old_tabs():
    match = re.search(
        r'<div class="tabs" id="primaryTabs">.*?(?=<div class="tabs hidden" id="operatorNav">)',
        INDEX_HTML, re.DOTALL,
    )
    assert match, "primaryTabs container is missing"
    block = match.group(0)
    assert 'data-tab="entry"' in block
    assert 'data-tab="history"' in block


# ---------- fully read-only state (Viewer always; Operator with no edit flags) ----------

def test_fully_read_only_condition_covers_viewer_and_unpermitted_operator():
    match = re.search(r"const isFullyReadOnly = isViewer \|\|\s*\(role === 'operator'.*?\);", INDEX_HTML, re.DOTALL)
    assert match, "isFullyReadOnly definition not found"
    body = match.group(0)
    assert "!operatorPermissions.can_edit_opening" in body
    assert "!operatorPermissions.can_edit_returns" in body
    assert "!operatorPermissions.can_edit_production" in body


def test_save_and_skip_buttons_not_rendered_when_fully_read_only():
    # Superseded by the Next Product/Previous Product split — see
    # tests/test_stage1_correction_next_product_review.py for the current
    # read-only nav-row assertions. This just pins that saveNextBtn/skipBtn
    # are still conditioned on isFullyReadOnly, not unconditional.
    assert '<button class="btn btn-primary" id="saveNextBtn">Save &amp; Next</button>' in INDEX_HTML
    assert "${isFullyReadOnly ? '' : `<button class=\"btn-skip\" id=\"skipBtn\">Skip — no activity for this product</button>`}" in INDEX_HTML


def test_read_only_message_matches_required_text():
    assert "Read-only — values are managed automatically or by an authorized manager." in INDEX_HTML


def test_no_save_request_possible_when_buttons_absent():
    """The click handlers are null-guarded — if isFullyReadOnly means the
    buttons were never rendered, there is nothing to attach a handler to
    and therefore no code path that can call saveCurrentAndAdvance()."""
    match = re.search(
        r"const saveNextBtn = document\.getElementById\('saveNextBtn'\);\s*"
        r"if\(saveNextBtn\) saveNextBtn\.addEventListener\('click', \(\)=>saveCurrentAndAdvance",
        INDEX_HTML,
    )
    assert match, "saveNextBtn click wiring must be null-guarded, not unconditional"
    skip_match = re.search(
        r"const skipBtn = document\.getElementById\('skipBtn'\);\s*"
        r"if\(skipBtn\) skipBtn\.addEventListener",
        INDEX_HTML,
    )
    assert skip_match, "skipBtn click wiring must be null-guarded, not unconditional"


def test_date_and_shift_filters_remain_unconditionally_available():
    assert '<input type="date" id="dateInput">' in INDEX_HTML
    assert 'id="shiftInput"' in INDEX_HTML
    # Neither is ever wrapped in an isFullyReadOnly/isViewer conditional.
    setup_block = re.search(r'<div class="setup">.*?</div>\s*</div>', INDEX_HTML, re.DOTALL).group(0)
    assert "isFullyReadOnly" not in setup_block
    assert "isViewer" not in setup_block


# ---------- Issued drill-down always available ----------

def test_issued_drilldown_never_conditionally_rendered():
    match = re.search(r'<button class="issued-link" id="issuedBreakdownBtn">view</button>', INDEX_HTML)
    assert match
    # Confirm it isn't wrapped in any ${...} ternary, unlike the adjust button.
    line_start = INDEX_HTML.rfind("\n", 0, match.start()) + 1
    line = INDEX_HTML[line_start:INDEX_HTML.find("\n", match.start())]
    assert "${" not in line


def test_adjust_button_remains_independently_gated_not_tied_to_fully_read_only():
    assert "${canAdjust ? `<button class=\"adjust-link\" id=\"adjustIssuedBtn\">adjust</button>` : ''}" in INDEX_HTML


# ---------- per-field editability untouched by this UI cleanup ----------

def test_per_field_permission_gating_still_intact():
    assert "const canEditOpening = isElevated || (role === 'operator' && operatorPermissions.can_edit_opening);" in INDEX_HTML
    assert "const canEditReturns = isElevated || (role === 'operator' && operatorPermissions.can_edit_returns);" in INDEX_HTML
    assert "const canEditProduction = isElevated || (role === 'operator' && operatorPermissions.can_edit_production);" in INDEX_HTML


def test_operator_with_one_permitted_field_still_gets_save_controls():
    """When isFullyReadOnly is false (at least one edit flag on), the
    ternary falls through to rendering the real Save & Next / Skip
    buttons — unauthorized individual fields stay disabled via
    canEditOpening/Returns/Production, proven separately above."""
    assert "${isFullyReadOnly ? '' : `<button" in INDEX_HTML
