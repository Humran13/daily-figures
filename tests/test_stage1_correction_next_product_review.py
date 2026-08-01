"""
Correction to the read-only Daily Figures UI: a fully read-only user
(Viewer always, or an Operator with none of the three edit flags on) must
still be able to move through and review every product — "Save & Next"
conflated saving with advancing, so simply hiding it also took away
review navigation. Split into a read-only "Previous Product"/"Next
Product" pair that only ever moves currentIdx, never calls the save API.

Source-level regression guards, same rationale as every other frontend-only
stage in this project (no JS/browser test runner exists here).
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _nav_row_block():
    # There are two <div class="nav-row"> blocks in this file — an earlier
    # one in the "no packaging rule configured" branch (always just a
    # Skip-only card, unrelated to role/permissions) and the real one that
    # holds the isFullyReadOnly ternary. Anchor on the marker that's unique
    # to the real one.
    match = re.search(r'<div class="nav-row">\s*\$\{isFullyReadOnly.*?</div>', INDEX_HTML, re.DOTALL)
    assert match, "the role-aware nav-row block is missing"
    return match.group(0)


# 1. Read-only users see "Next Product", not "Save & Next"

def test_read_only_branch_shows_next_product_not_save_and_next():
    block = _nav_row_block()
    read_only_branch = re.search(r"\$\{isFullyReadOnly \? `(.*?)` : `", block, re.DOTALL)
    assert read_only_branch, "read-only branch of the nav-row ternary not found"
    branch = read_only_branch.group(1)
    assert 'id="nextProductBtn">Next Product<' in branch
    assert "Save &amp; Next" not in branch
    assert "saveNextBtn" not in branch


def test_editable_branch_still_shows_save_and_next():
    block = _nav_row_block()
    editable_branch = re.search(r"` : `(.*?)`\}", block, re.DOTALL)
    assert editable_branch, "editable branch of the nav-row ternary not found"
    branch = editable_branch.group(1)
    assert 'id="saveNextBtn">Save &amp; Next<' in branch
    assert "nextProductBtn" not in branch


# 2. Clicking "Next Product" does not call any write API

def test_next_product_click_handler_is_pure_navigation():
    match = re.search(
        r"const nextProductBtn = document\.getElementById\('nextProductBtn'\);\s*"
        r"if\(nextProductBtn\) nextProductBtn\.addEventListener\('click', \(\)=>\{([^}]*)\}\);",
        INDEX_HTML,
    )
    assert match, "nextProductBtn click handler not found or not null-guarded"
    body = match.group(1)
    # Stage 7 added a fire-and-forget lock release (releaseLockIfOwned) —
    # still no direct write API call from this handler itself; the release
    # is a no-op for anyone who never held a lock (Manager/Super Admin/
    # Viewer, or an Operator on a read-only row) and best-effort otherwise.
    assert body.strip() == "releaseLockIfOwned(product, date, shift); currentIdx++; renderEntryCard();"
    assert "api(" not in body and "apiPost(" not in body and "fetch(" not in body


def test_previous_product_click_handler_is_pure_navigation():
    """
    prevProductBtn is now shared by both branches and gained an
    unsaved-changes confirm() for editable users — see
    tests/test_stage1_correction_previous_product_symmetry.py for the full
    behavior. This just pins that it still never issues a write request.
    """
    match = re.search(
        r"const prevProductBtn = document\.getElementById\('prevProductBtn'\);\s*"
        r"if\(prevProductBtn\) prevProductBtn\.addEventListener\('click', \(\)=>\{(.*?)\n  \}\);",
        INDEX_HTML, re.DOTALL,
    )
    assert match, "prevProductBtn click handler not found or not null-guarded"
    body = match.group(1)
    assert "currentIdx--; renderEntryCard();" in body
    assert "api(" not in body and "apiPost(" not in body and "fetch(" not in body


# 3. Previous/Next allow reviewing all products (visibility pattern matches the old Back button)

def test_previous_product_only_hidden_on_first_product_like_back_used_to_be():
    block = _nav_row_block()
    assert 'currentIdx>0 ? `<button class="btn btn-ghost" id="prevProductBtn">Previous Product</button>` : \'\'' in block


def test_next_product_hidden_on_last_product_in_read_only_branch():
    """Superseded expectation: Next Product is now hidden on the last
    product (new requirement) rather than always rendered — see
    tests/test_stage1_correction_previous_product_symmetry.py."""
    read_only_branch = re.search(r"\$\{isFullyReadOnly \? `(.*?)` : `", _nav_row_block(), re.DOTALL).group(1)
    assert 'currentIdx<products.length-1 ? `<button class="btn btn-primary" id="nextProductBtn">Next Product</button>` : \'\'' in read_only_branch


# 4. Progress indicator updates independent of read-only branching

def test_progress_indicator_set_unconditionally_before_role_branching():
    idx_progress = INDEX_HTML.index("progressLabel").__index__()
    idx_readonly = INDEX_HTML.index("isFullyReadOnly")
    assert idx_progress < idx_readonly, "progress indicator must be computed before the read-only branch, not conditioned on it"
    assert "document.getElementById('progressLabel').textContent = (currentIdx+1)+' / '+products.length;" in INDEX_HTML


# 5. Issued drill-down remains available regardless of read-only state

def test_issued_drilldown_button_not_conditioned_on_read_only():
    match = re.search(r'<button class="issued-link" id="issuedBreakdownBtn">view</button>', INDEX_HTML)
    assert match
    line_start = INDEX_HTML.rfind("\n", 0, match.start()) + 1
    line = INDEX_HTML[line_start:INDEX_HTML.find("\n", match.start())]
    assert "${" not in line  # never wrapped in a conditional


# 6. Skip is hidden in read-only mode

def test_skip_button_hidden_when_fully_read_only():
    # Stage 7 renamed Skip's label to disambiguate it from the new explicit
    # "No Activity Today" completion (see tests/test_stage7_daily_entry_ownership.py)
    # — Skip means "come back later, not reviewed", never "reviewed as zero".
    assert '${isFullyReadOnly ? \'\' : `<button class="btn-skip" id="skipBtn">Skip for now' in INDEX_HTML


def test_read_only_branch_of_nav_row_contains_no_skip_reference():
    read_only_branch = re.search(r"\$\{isFullyReadOnly \? `(.*?)` : `", _nav_row_block(), re.DOTALL).group(1)
    assert "skipBtn" not in read_only_branch


# 7. Editable Manager/Super Admin workflow unchanged

def test_editable_workflow_handlers_unchanged():
    # "Back" was later unified into the shared "Previous Product" button —
    # see tests/test_stage1_correction_previous_product_symmetry.py. Save &
    # Next and Skip are untouched by that change.
    # Stage 8 section 3 added a showOpeningInputs parameter so a Manager/
    # Super Admin correction on a later (normally-derived) period is also
    # submitted correctly — see tests/test_stage8_stock_carry_forward.py.
    assert "if(saveNextBtn) saveNextBtn.addEventListener('click', ()=>saveCurrentAndAdvance(product, date, shift, rule, view, showOpeningInputs));" in INDEX_HTML
    # Stage 7 added a fire-and-forget lock release before advancing.
    assert "if(skipBtn) skipBtn.addEventListener('click', ()=>{ releaseLockIfOwned(product, date, shift); currentIdx++; renderEntryCard(); });" in INDEX_HTML


# 8. isFullyReadOnly is false only when this is a first-ever entry (or an
# elevated correction — Stage 8 section 3's showOpeningInputs) the current
# role is allowed to set Opening Stock on — Stage 5 removed the separate
# Return/Production edit flags entirely (both are now always read-only,
# sourced from their own Books, for every role), so read-only no longer
# branches on three separately-negated permission flags.

def test_is_fully_read_only_depends_only_on_opening_editability_and_permission():
    match = re.search(r"const isFullyReadOnly = isViewer \|\| !\((.*?)\);", INDEX_HTML)
    assert match
    condition = match.group(1)
    assert condition == "showOpeningInputs && canEditOpening"
