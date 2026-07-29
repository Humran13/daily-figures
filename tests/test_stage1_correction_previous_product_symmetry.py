"""
Adds symmetric Previous/Next Product navigation:

- Read-only review mode: Previous Product hidden on the first product,
  Next Product now hidden on the LAST product too (previously always
  rendered, which would loop past the end instead of stopping at it).
- Editable mode: the old "Back" button is unified into the same
  "Previous Product" button/id used by read-only mode. It still never
  saves anything, but now warns (via confirm()) before discarding
  in-progress typed changes instead of silently dropping them.

Source-level regression guards, same rationale as every other frontend-only
stage in this project (no JS/browser test runner exists here).
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _nav_row_block():
    match = re.search(r'<div class="nav-row">\s*\$\{isFullyReadOnly.*?</div>', INDEX_HTML, re.DOTALL)
    assert match, "the role-aware nav-row block is missing"
    return match.group(0)


def _prev_product_handler_body():
    match = re.search(
        r"if\(prevProductBtn\) prevProductBtn\.addEventListener\('click', \(\)=>\{(.*?)\n  \}\);",
        INDEX_HTML, re.DOTALL,
    )
    assert match, "prevProductBtn click handler not found"
    return match.group(1)


# ---------- 1 & 2: Previous and Next move correctly ----------

def test_next_product_increments_current_index():
    body = re.search(
        r"if\(nextProductBtn\) nextProductBtn\.addEventListener\('click', \(\)=>\{([^}]*)\}\);", INDEX_HTML
    ).group(1)
    assert body.strip() == "currentIdx++; renderEntryCard();"


def test_previous_product_decrements_current_index():
    assert "currentIdx--; renderEntryCard();" in _prev_product_handler_body()


# ---------- boundary behavior on first and last product ----------

def test_previous_product_hidden_on_first_product():
    block = _nav_row_block()
    assert 'currentIdx>0 ? `<button class="btn btn-ghost" id="prevProductBtn">Previous Product</button>` : \'\'' in block


def test_next_product_hidden_on_last_product_read_only():
    read_only_branch = re.search(r"\$\{isFullyReadOnly \? `(.*?)` : `", _nav_row_block(), re.DOTALL).group(1)
    assert 'currentIdx<products.length-1 ? `<button class="btn btn-primary" id="nextProductBtn">Next Product</button>` : \'\'' in read_only_branch


def test_previous_product_present_and_gated_identically_in_both_branches():
    block = _nav_row_block()
    assert block.count('id="prevProductBtn">Previous Product</button>') == 2
    assert block.count('currentIdx>0 ? `<button class="btn btn-ghost" id="prevProductBtn">Previous Product</button>` : \'\'') == 2


# ---------- progress indicator ----------

def test_progress_indicator_unaffected_by_this_correction():
    assert "document.getElementById('progressFill').style.width = ((currentIdx)/products.length*100)+'%';" in INDEX_HTML
    assert "document.getElementById('progressLabel').textContent = (currentIdx+1)+' / '+products.length;" in INDEX_HTML


# ---------- no write API in read-only mode ----------

def test_next_and_previous_never_call_a_write_api():
    for body in (
        re.search(r"if\(nextProductBtn\) nextProductBtn\.addEventListener\('click', \(\)=>\{([^}]*)\}\);", INDEX_HTML).group(1),
        _prev_product_handler_body(),
    ):
        for verb in ("api(", "apiPost(", "fetch(", "saveCurrentAndAdvance"):
            assert verb not in body, f"{verb} must not appear in read-only navigation handlers"


def test_date_and_shift_filters_preserved_across_navigation():
    # Date/Shift live in the always-present #dateInput/#shiftInput fields,
    # read fresh by renderEntryCard() on every call — currentIdx changes
    # never touch them.
    assert "const date = document.getElementById('dateInput').value;" in INDEX_HTML
    assert "const shift = document.getElementById('shiftInput').value;" in INDEX_HTML
    handler_span = INDEX_HTML[INDEX_HTML.index("const prevProductBtn"):INDEX_HTML.index("function hasUnsavedChanges")]
    assert "dateInput" not in handler_span and "shiftInput" not in handler_span


# ---------- Issued drill-down / read-only fields untouched ----------

def test_issued_drilldown_still_unconditional():
    assert '<button class="issued-link" id="issuedBreakdownBtn">view</button>' in INDEX_HTML


def test_skip_still_hidden_in_read_only_mode():
    assert "${isFullyReadOnly ? '' : `<button class=\"btn-skip\" id=\"skipBtn\">Skip — no activity for this product</button>`}" in INDEX_HTML


# ---------- editable workflow: Previous Product added, no auto-save, warns if dirty ----------

def test_editable_branch_also_shows_previous_product():
    editable_branch = re.search(r"` : `(.*?)`\}", _nav_row_block(), re.DOTALL).group(1)
    assert 'id="prevProductBtn">Previous Product<' in editable_branch
    assert "backBtn" not in editable_branch
    assert 'id="saveNextBtn">Save &amp; Next<' in editable_branch  # unchanged


def test_previous_product_does_not_autosave():
    body = _prev_product_handler_body()
    assert "saveCurrentAndAdvance" not in body


def test_previous_product_warns_before_discarding_unsaved_changes():
    body = _prev_product_handler_body()
    assert "!isFullyReadOnly && hasUnsavedChanges(rule, view)" in body
    assert "confirm(" in body
    assert "if(!confirm(" in body and "return;" in body  # cancelling must stop navigation


def test_has_unsaved_changes_checks_only_editable_opening():
    """
    Stage 5 moved Return/Production entry to their own Books — Daily
    Figures no longer has editable inputs for either, so
    hasUnsavedChanges() has nothing left to check but Opening Stock (and
    only when it's actually editable, i.e. a product's first-ever period).
    """
    match = re.search(r"function hasUnsavedChanges\(rule, view\)\{(.*?)\n\}", INDEX_HTML, re.DOTALL)
    assert match, "hasUnsavedChanges helper is missing"
    body = match.group(1)
    assert "readQtyInputs('ret', rule)" not in body
    assert "readQtyInputs('prod', rule)" not in body
    assert "if(!view.opening_editable) return false;" in body
    assert "differs(readQtyInputs('opening', rule), view.opening)" in body


def test_save_and_next_workflow_completely_unchanged():
    assert "if(saveNextBtn) saveNextBtn.addEventListener('click', ()=>saveCurrentAndAdvance(product, date, shift, rule, view));" in INDEX_HTML
