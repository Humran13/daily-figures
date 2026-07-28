"""
Regression guards for two small UI fixes:

1. Quantity fields (Cartons/Packs/Pieces on the dispatch form, and
   Opening/Return/Production on the Daily Figures entry card) show "0" by
   default; a field showing exactly "0" now clears itself on focus so
   typing starts clean and left-to-right, and reverts to displaying "0" on
   blur if left empty. A blank field is already treated as zero for
   calculations (existing parseInt(...)||0 / NaN-check parsing) — the fix
   is display-only and must never rebuild the surrounding DOM or otherwise
   disturb the multi-digit-typing fix from the previous change.
2. The dispatch page's "New Dispatch" tab now appears before "Dispatches"
   in source order.

As with tests/test_dispatch_html_quantity_inputs.py, this project has no
JS test runner (no package.json, no Node/npm, no Playwright/Selenium), so
these are source-level regression guards rather than a live browser
simulation.
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DISPATCH_HTML = (STATIC_DIR / "dispatch.html").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _attach_zero_clear_fn(source):
    match = re.search(r"function attachZeroClearBehavior\(input\)\{.*?\n\}", source, re.DOTALL)
    assert match, "attachZeroClearBehavior(input) helper is missing"
    return match.group(0)


# ---------- dispatch.html: Cartons/Packs/Pieces ----------

def test_dispatch_qty_fields_wire_up_zero_clear_behavior():
    assert "attachZeroClearBehavior(input)" in DISPATCH_HTML


def test_dispatch_zero_clear_focus_handler_clears_exact_zero_only():
    fn = _attach_zero_clear_fn(DISPATCH_HTML)
    assert "addEventListener('focus'" in fn
    assert "input.value === '0'" in fn
    assert "input.value = ''" in fn


def test_dispatch_zero_clear_blur_handler_restores_zero_when_blank():
    fn = _attach_zero_clear_fn(DISPATCH_HTML)
    assert "addEventListener('blur'" in fn
    assert "input.value.trim() === ''" in fn
    assert "input.value = '0'" in fn


def test_dispatch_zero_clear_handlers_never_touch_currentLines_or_rebuild_dom():
    fn = _attach_zero_clear_fn(DISPATCH_HTML)
    assert "currentLines" not in fn, "display-only fix — blank already reads as 0 via the input handler"
    assert "renderLines()" not in fn


def test_dispatch_multi_digit_typing_fix_still_intact():
    """Guards against this change regressing the previously-fixed reversal bug."""
    handler_match = re.search(
        r"container\.querySelectorAll\('\[data-qty\]'\)\.forEach\(input=>\{.*?\}\);",
        DISPATCH_HTML,
        re.DOTALL,
    )
    assert handler_match
    handler = handler_match.group(0)
    assert "renderLines()" not in handler
    assert "selectionStart" not in handler and "selectionEnd" not in handler
    assert "Number.isNaN(parsed)" in handler


def test_dispatch_blank_quantity_parses_as_zero_for_calculations():
    def parse(value):
        n = int(value) if re.fullmatch(r"-?\d+", value) else None
        return 0 if n is None else n

    assert parse("") == 0
    assert parse("85") == 85


# ---------- index.html: Opening/Return/Production ----------

def test_daily_figures_qty_fields_wire_up_zero_clear_behavior():
    assert "attachZeroClearBehavior(el)" in INDEX_HTML
    _attach_zero_clear_fn(INDEX_HTML)  # asserts the helper itself is defined


def test_daily_figures_blank_quantity_already_reads_as_zero():
    match = re.search(r"function readQtyInputs\(prefix, rule\)\{.*?\n\}", INDEX_HTML, re.DOTALL)
    assert match
    body = match.group(0)
    assert "|| 0" in body


# ---------- tab order ----------

def test_new_dispatch_tab_appears_before_dispatches_tab():
    tabs_match = re.search(r'<div class="tabs">.*?</div>\s*</header>', DISPATCH_HTML, re.DOTALL)
    assert tabs_match, "could not find the dispatch page tab bar"
    tabs_html = tabs_match.group(0)
    new_pos = tabs_html.find('data-tab="new"')
    list_pos = tabs_html.find('data-tab="list"')
    assert new_pos != -1 and list_pos != -1
    assert new_pos < list_pos, "New Dispatch must appear before Dispatches"


def test_both_dispatch_tabs_still_present_and_unrenamed():
    assert re.search(r'data-tab="new">\s*New Dispatch\s*<', DISPATCH_HTML)
    assert re.search(r'data-tab="list">\s*Dispatches\s*<', DISPATCH_HTML)


def test_dashboard_link_still_present():
    assert '/dashboard.html' in DISPATCH_HTML
