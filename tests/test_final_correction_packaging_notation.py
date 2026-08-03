"""
Final pre-deployment correction, Part 1: packaging-aware book-style point
notation for carton-plus-piece products (no pack tier — KingMax,
JumboMax, Kitchen Towel Singles, and any future product configured the
same way).

Before this correction, webapp/services/quantity_format.qty_label()
rendered a no-pack-tier product as "Xc Ypc" (e.g. "0c 0pc", "5c 3pc"),
which doesn't match the physical stock book's point notation. It now
uses the same "C.PP Ctns" positional notation as pack-tier products,
where the digits after the point are the loose-piece remainder — zero-
padded to a width derived from the product's own carton capacity
(carton_to_pieces), never hard-coded per product name.

Three-tier (carton+pack+piece) notation is completely unchanged — see
tests/test_stage5_book_notation.py for its own full spec, still passing
unmodified assertions.

This is a display-only change. No stock calculation, storage type, or
carry-forward behavior is touched — see
tests/test_stage8_production_hotfix.py and friends for that coverage,
re-run in full as part of this correction's regression pass.
"""
import inspect
from pathlib import Path

import pytest

from webapp.services.quantity_format import qty_label
from webapp.services.packaging import from_base_units, normalize, to_base_units

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
QUANTITY_FORMAT_JS = (STATIC_DIR / "quantity_format.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
HISTORY_HTML = (STATIC_DIR / "history.html").read_text(encoding="utf-8")

KINGMAX = {"cartons_to_packs": None, "packs_to_pieces": None, "carton_to_pieces": 60}
JUMBOMAX = {"cartons_to_packs": None, "packs_to_pieces": None, "carton_to_pieces": 24}
KITCHEN_TOWEL_SINGLES = {"cartons_to_packs": None, "packs_to_pieces": None, "carton_to_pieces": 24}
FUTURE_PRODUCT = {"cartons_to_packs": None, "packs_to_pieces": None, "carton_to_pieces": 8}
COMPACT = {"cartons_to_packs": 10, "packs_to_pieces": 10, "carton_to_pieces": None}
NAPKIN = {"cartons_to_packs": 6, "packs_to_pieces": 10, "carton_to_pieces": None}
STRAWS = {"cartons_to_packs": 12, "packs_to_pieces": 100, "carton_to_pieces": None}


# =====================================================================
# Carton-plus-piece products — the exact examples from the spec
# =====================================================================

def test_zero_displays_as_two_digit_point_notation_not_old_format():
    assert qty_label(0, 0, 0, KINGMAX) == "0.00 Ctns"


def test_five_cartons_three_pieces():
    assert qty_label(5, 0, 3, KINGMAX) == "5.03 Ctns"
    assert qty_label(5, 0, 3, JUMBOMAX) == "5.03 Ctns"


def test_kingmax_twelve_cartons_twenty_five_pieces():
    assert qty_label(12, 0, 25, KINGMAX) == "12.25 Ctns"


def test_kingmax_sixty_loose_pieces_carries_into_one_carton():
    # 12 cartons + 60 pieces normalizes (via packaging.py, upstream of the
    # formatter) to 13 cartons + 0 pieces before qty_label ever sees it.
    cartons, packs, pieces = normalize(12, 0, 60, _Rule(KINGMAX))
    assert (cartons, packs, pieces) == (13, 0, 0)
    assert qty_label(cartons, packs, pieces, KINGMAX) == "13.00 Ctns"


def test_jumbomax_twenty_three_loose_pieces_remains_dot_23():
    assert qty_label(12, 0, 23, JUMBOMAX) == "12.23 Ctns"


def test_jumbomax_twenty_four_loose_pieces_carries_into_one_carton():
    cartons, packs, pieces = normalize(12, 0, 24, _Rule(JUMBOMAX))
    assert (cartons, packs, pieces) == (13, 0, 0)
    assert qty_label(cartons, packs, pieces, JUMBOMAX) == "13.00 Ctns"


def test_jumbomax_twenty_five_loose_pieces_normalizes_to_carton_plus_one():
    cartons, packs, pieces = normalize(12, 0, 25, _Rule(JUMBOMAX))
    assert (cartons, packs, pieces) == (13, 0, 1)
    assert qty_label(cartons, packs, pieces, JUMBOMAX) == "13.01 Ctns"  # not "12.25"


def test_kitchen_towel_singles_uses_configured_carton_plus_piece_rule():
    assert qty_label(2, 0, 5, KITCHEN_TOWEL_SINGLES) == "2.05 Ctns"


def test_future_carton_plus_piece_product_automatically_uses_point_notation():
    """A product no test has ever seen before, with its own arbitrary
    carton_to_pieces, must get point notation purely from its packaging
    rule — nothing here is specific to KingMax/JumboMax by name."""
    assert qty_label(3, 0, 7, FUTURE_PRODUCT) == "3.07 Ctns"


# =====================================================================
# Digit width is derived from the product's own carton capacity
# =====================================================================

def test_remainder_width_is_at_least_two_digits_even_for_small_capacity():
    small = {"cartons_to_packs": None, "packs_to_pieces": None, "carton_to_pieces": 5}
    assert qty_label(1, 0, 3, small) == "1.03 Ctns"  # not "1.3"


def test_remainder_width_grows_for_a_three_digit_capacity():
    big = {"cartons_to_packs": None, "packs_to_pieces": None, "carton_to_pieces": 500}
    assert qty_label(2, 0, 7, big) == "2.007 Ctns"
    assert qty_label(2, 0, 123, big) == "2.123 Ctns"


# =====================================================================
# Input normalization (packaging.py) is untouched — carry the exact
# examples from the spec through normalize()/to_base_units() end to end
# =====================================================================

class _Rule:
    """Adapts a plain dict (as used throughout this test file and by
    stock_service.daily_figure_view()'s packaging_rule dict) to the
    attribute-style access packaging.py's normalize()/to_base_units()
    expect from a real PackagingRule row."""
    def __init__(self, d):
        self.cartons_to_packs = d["cartons_to_packs"]
        self.packs_to_pieces = d["packs_to_pieces"]
        self.carton_to_pieces = d["carton_to_pieces"]

    @property
    def has_pack_tier(self):
        return self.cartons_to_packs is not None


def test_kingmax_two_cartons_sixty_three_pieces_normalizes_to_three_three():
    cartons, packs, pieces = normalize(2, 0, 63, _Rule(KINGMAX))
    assert (cartons, packs, pieces) == (3, 0, 3)
    assert qty_label(cartons, packs, pieces, KINGMAX) == "3.03 Ctns"


def test_jumbomax_two_cartons_twenty_five_pieces_normalizes_to_three_one():
    cartons, packs, pieces = normalize(2, 0, 25, _Rule(JUMBOMAX))
    assert (cartons, packs, pieces) == (3, 0, 1)
    assert qty_label(cartons, packs, pieces, JUMBOMAX) == "3.01 Ctns"


def test_negative_component_inputs_still_rejected():
    from webapp.services.packaging import PackagingError
    with pytest.raises(PackagingError):
        normalize(-1, 0, 5, _Rule(KINGMAX))
    with pytest.raises(PackagingError):
        normalize(2, 0, -5, _Rule(KINGMAX))


# =====================================================================
# Exact integer storage — no floats anywhere in the formatter or the
# underlying conversion
# =====================================================================

def test_formatter_still_never_uses_floating_point_or_rounding():
    source = inspect.getsource(qty_label)
    assert "float(" not in source
    assert "round(" not in source


def test_round_trip_through_base_units_is_exact_no_precision_loss():
    for rule_dict, cartons, pieces in [
        (KINGMAX, 12, 25), (JUMBOMAX, 12, 23), (KINGMAX, 0, 0), (JUMBOMAX, 100, 23),
    ]:
        rule = _Rule(rule_dict)
        base = to_base_units(cartons, 0, pieces, rule)
        assert isinstance(base, int)
        back_c, back_p, back_pc = from_base_units(base, rule)
        assert (back_c, back_p, back_pc) == (cartons, 0, pieces)
        assert qty_label(back_c, back_p, back_pc, rule_dict) == qty_label(cartons, 0, pieces, rule_dict)


def test_base_unit_columns_remain_sql_integer_not_real_or_float():
    from webapp.models.dispatch import DispatchLine
    from webapp.models.return_record import ReturnLine
    from webapp.models.production_record import ProductionLine
    from webapp.models.daily_figure import DailyFigure
    for model, col_name in (
        (DispatchLine, "base_unit_qty"), (ReturnLine, "base_unit_qty"), (ProductionLine, "base_unit_qty"),
        (DailyFigure, "opening_base_qty"),
    ):
        col = model.__table__.columns[col_name]
        assert col.type.__class__.__name__ == "Integer"


# =====================================================================
# Three-tier products completely unchanged
# =====================================================================

def test_compact_notation_unchanged():
    assert qty_label(5, 0, 0, COMPACT) == "5 Ctns"
    assert qty_label(5, 6, 8, COMPACT) == "5.68 Ctns"


def test_napkin_mixed_radix_notation_unchanged():
    assert qty_label(1, 2, 4, NAPKIN) == "1.24 Ctns"


def test_straws_configured_structure_unchanged():
    assert qty_label(1, 3, 7, STRAWS) == "1.37 Ctns"  # fits single digit
    assert qty_label(5, 11, 45, STRAWS) == "5c 11p 45pc"  # falls back, unchanged


def test_no_pack_tier_products_never_get_a_fake_pack_slot():
    # Confirms the point notation for carton+piece products is a single
    # positional slot (the remainder), never a synthesized "packs" digit.
    # Anchored to the POSITIVE no-pack-tier branch specifically (its own
    # unique "carton_to_pieces" line right after the guard) — final
    # legacy-migration investigation, section 9 added an EARLIER negative-
    # quantity branch that also mentions "if not _has_pack_tier", which a
    # looser split would incorrectly capture too.
    source = inspect.getsource(qty_label)
    positive_no_pack_tier_branch = source.split("if not _has_pack_tier(rule):\n        carton_to_pieces")[1].split("if packs == 0")[0]
    assert "packs" not in positive_no_pack_tier_branch


# =====================================================================
# No floating-point artifacts appear anywhere
# =====================================================================

def test_no_floating_point_artifact_in_any_label():
    for rule_dict, c, p, pc in [(KINGMAX, 5, 0, 3), (JUMBOMAX, 12, 0, 25), (COMPACT, 5, 6, 8), (NAPKIN, 1, 2, 4)]:
        label = qty_label(c, p, pc, rule_dict)
        assert "." in label or "Ctns" in label
        assert "e-" not in label.lower()  # no scientific notation, no float repr artifact


# =====================================================================
# Cross-screen consistency — one centralized formatter everywhere
# =====================================================================

def test_daily_figures_index_uses_the_shared_formatter_not_a_local_copy():
    assert '<script src="/quantity_format.js"></script>' in INDEX_HTML
    assert "function qtyLabel(part, rule){" not in INDEX_HTML  # no local duplicate left behind
    assert "function qtyLabel(part, rule){" in QUANTITY_FORMAT_JS


def test_history_page_uses_the_shared_formatter_not_a_local_copy():
    assert '<script src="/quantity_format.js"></script>' in HISTORY_HTML
    assert "function qtyLabel(part, rule){" not in HISTORY_HTML
    assert "function hasPackTier(rule){" not in HISTORY_HTML


def test_shared_js_formatter_matches_python_formatter_carton_plus_piece_examples():
    """Source-level parity check: the JS remainder-width computation must
    use the same `max(2, digits of carton_to_pieces-1)` rule as Python's
    qty_label(), not a hard-coded width."""
    idx = QUANTITY_FORMAT_JS.index("function qtyLabel(part, rule){")
    body = QUANTITY_FORMAT_JS[idx:QUANTITY_FORMAT_JS.index("\n}", idx)]
    assert "Math.max(2," in body
    assert "padStart(width, '0')" in body


def test_dispatch_returns_production_pages_render_backend_computed_label():
    """These three pages never had their own JS qtyLabel copy — they
    display the backend's to_dict()-computed quantity_label verbatim, so
    they pick up the corrected point notation automatically with zero
    frontend change required."""
    for page in ("dispatch.html", "production.html", "returns.html"):
        html = (STATIC_DIR / page).read_text(encoding="utf-8")
        assert "function qtyLabel(" not in html
        assert "l.quantity_label" in html or "quantity_label" in html
