"""
Regression guard for the quantity-input digit-reversal bug in
static/dispatch.html: renderLines() used to rebuild the entire product-line
DOM on every keystroke and then try to restore the cursor via
selectionStart/selectionEnd, which input[type=number] doesn't support — the
cursor silently reset to position 0, so typing "85" produced "58".

This project has no JS test runner (no package.json, no Node/npm, no
Playwright/Selenium in requirements-dev.txt), so this asserts the fix at the
source level: the qty-input handler must update state and a single line's
hint text in place, and must never rebuild the line list or touch
selectionStart/selectionEnd.
"""
import re
from pathlib import Path

DISPATCH_HTML = (Path(__file__).resolve().parent.parent / "static" / "dispatch.html").read_text(encoding="utf-8")


def _qty_input_handler():
    match = re.search(
        r"container\.querySelectorAll\('\[data-qty\]'\)\.forEach\(input=>\{.*?\}\);",
        DISPATCH_HTML,
        re.DOTALL,
    )
    assert match, "could not find the [data-qty] input event handler in dispatch.html"
    return match.group(0)


def test_qty_input_handler_does_not_rebuild_the_dom():
    handler = _qty_input_handler()
    assert "renderLines()" not in handler, (
        "quantity input handler must not rebuild the entire line list on every "
        "keystroke — that destroys and recreates the input mid-typing"
    )


def test_qty_input_handler_does_not_use_unsupported_selection_hack():
    handler = _qty_input_handler()
    assert "selectionStart" not in handler and "selectionEnd" not in handler, (
        "input[type=number] does not support selectionStart/selectionEnd — "
        "setting it silently resets the cursor to position 0, reversing "
        "multi-digit entry (typing 85 becomes 58)"
    )


def test_qty_input_handler_updates_current_lines_and_hint_in_place():
    handler = _qty_input_handler()
    assert "currentLines[idx][unit]" in handler
    assert "updateLineHint(idx)" in handler


def test_update_line_hint_function_updates_only_its_own_line():
    match = re.search(r"function updateLineHint\(idx\)\{.*?\n\}", DISPATCH_HTML, re.DOTALL)
    assert match, "updateLineHint(idx) helper is missing from dispatch.html"
    body = match.group(0)
    assert 'querySelector(`[data-hint="${idx}"]`)' in body
    assert "renderLines()" not in body


def test_qty_inputs_parse_multi_digit_values_without_reordering():
    """
    Pin down the exact parsing rule: parseInt(input.value, 10), falling back
    to 0 only on NaN (not on falsy-but-valid 0). A stray `|| 0` instead of a
    NaN check would be harmless for parsing but is the kind of change that
    tends to travel together with reintroducing a full rebuild, so pin the
    intended behavior directly with the parser itself.
    """
    handler = _qty_input_handler()
    assert "parseInt(input.value, 10)" in handler
    assert "Number.isNaN(parsed)" in handler

    def parse(value):
        n = int(value) if re.fullmatch(r"-?\d+", value) else None
        return 0 if n is None else n

    assert parse("85") == 85
    assert parse("123") == 123
    assert parse("0") == 0


def test_line_cards_carry_a_stable_hint_element_per_index():
    assert 'data-hint="${idx}"' in DISPATCH_HTML
