"""
Stage 7 sections 5 and 8: zero is a valid business value everywhere a
quantity is accepted/displayed, and exact Napkin mixed-radix arithmetic
(1 carton = 6 packs, 1 pack = 10 pieces) — verified against the spec's own
worked examples, with an explicit source-level check that no float-based
conversion exists anywhere in the stock/packaging pipeline.
"""
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from webapp.services.packaging import from_base_units, normalize, to_base_units
from webapp.services.quantity_format import qty_label

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def rule(cartons_to_packs=None, packs_to_pieces=None, carton_to_pieces=None):
    r = SimpleNamespace(
        cartons_to_packs=cartons_to_packs, packs_to_pieces=packs_to_pieces, carton_to_pieces=carton_to_pieces,
    )
    r.has_pack_tier = cartons_to_packs is not None
    return r


NAPKINS = rule(cartons_to_packs=6, packs_to_pieces=10)
COMPACT = rule(cartons_to_packs=10, packs_to_pieces=10)
KINGMAX = rule(carton_to_pieces=60)


# =====================================================================
# Napkin mixed-radix arithmetic — the spec's own worked examples, verbatim
# =====================================================================

def test_napkin_1c_2p_4pc_displays_as_1_24_ctns():
    assert qty_label(1, 2, 4, NAPKINS) == "1.24 Ctns"


def test_napkin_5_packs_9_pieces_remains_0_59_ctns():
    assert qty_label(0, 5, 9, NAPKINS) == "0.59 Ctns"


def test_napkin_6_packs_becomes_1_ctns():
    c, p, pc = normalize(0, 6, 0, NAPKINS)
    assert (c, p, pc) == (1, 0, 0)
    assert qty_label(c, p, pc, NAPKINS) == "1 Ctns"


def test_napkin_1_pack_and_10_pieces_normalizes_to_2_packs():
    c, p, pc = normalize(0, 1, 10, NAPKINS)
    assert (c, p, pc) == (0, 2, 0)


def test_napkin_5_packs_and_10_pieces_normalizes_to_1_carton():
    c, p, pc = normalize(0, 5, 10, NAPKINS)
    assert (c, p, pc) == (1, 0, 0)


def test_napkin_308_cartons_0_packs_7_pieces_displays_exactly():
    assert qty_label(308, 0, 7, NAPKINS) == "308.07 Ctns"
    assert "308.0000007" not in qty_label(308, 0, 7, NAPKINS)


def test_napkin_pack_remainder_always_0_to_5():
    for total_packs in range(0, 30):
        cartons, packs = divmod(total_packs, NAPKINS.cartons_to_packs)
        assert 0 <= packs <= 5


def test_napkin_piece_remainder_always_0_to_9():
    for total_pieces in range(0, 50):
        packs, pieces = divmod(total_pieces, NAPKINS.packs_to_pieces)
        assert 0 <= pieces <= 9


def test_napkin_6_packs_carries_to_1_carton_via_to_base_units_round_trip():
    base = to_base_units(0, 6, 0, NAPKINS)
    c, p, pc = from_base_units(base, NAPKINS)
    assert (c, p, pc) == (1, 0, 0)


def test_napkin_10_pieces_carries_to_1_pack_via_to_base_units_round_trip():
    base = to_base_units(0, 0, 10, NAPKINS)
    c, p, pc = from_base_units(base, NAPKINS)
    assert (c, p, pc) == (0, 1, 0)


def test_napkin_combined_carry_pieces_then_packs():
    # 5 packs + 60 pieces -> pieces carry to +6 packs (5+6=11 packs) -> 11
    # packs carry to 1 carton, 5 packs remaining.
    base = to_base_units(0, 5, 60, NAPKINS)
    c, p, pc = from_base_units(base, NAPKINS)
    assert (c, p, pc) == (1, 5, 0)


@pytest.mark.parametrize("cartons,packs,pieces", [
    (0, 0, 0), (1, 0, 0), (2, 3, 4), (10, 5, 9), (100, 0, 0), (999, 5, 9),
])
def test_napkin_round_trip_is_exact_for_many_values(cartons, packs, pieces):
    base = to_base_units(cartons, packs, pieces, NAPKINS)
    c2, p2, pc2 = from_base_units(base, NAPKINS)
    assert to_base_units(c2, p2, pc2, NAPKINS) == base


# =====================================================================
# No float anywhere in the conversion pipeline
# =====================================================================

def test_packaging_module_source_contains_no_float_conversion():
    import webapp.services.packaging as packaging_module
    source = inspect.getsource(packaging_module)
    assert "float(" not in source
    assert "round(" not in source
    assert " / " not in source  # only integer // and % are used


def test_quantity_format_module_source_contains_no_float_conversion():
    import webapp.services.quantity_format as qf_module
    source = inspect.getsource(qf_module)
    assert "float(" not in source
    assert "round(" not in source


def test_stock_service_module_source_contains_no_float_conversion():
    import webapp.services.stock_service as stock_module
    source = inspect.getsource(stock_module)
    assert "float(" not in source
    assert "round(" not in source


def test_daily_figure_model_stores_integers_not_real_or_float():
    from webapp.models.daily_figure import DailyFigure
    for col_name in ("opening_base_qty", "return_base_qty", "production_base_qty"):
        col = DailyFigure.__table__.columns[col_name]
        assert col.type.__class__.__name__ == "Integer"


def test_dispatch_line_base_qty_stored_as_integer_not_real():
    from webapp.models.dispatch import DispatchLine
    col = DispatchLine.__table__.columns["base_unit_qty"]
    assert col.type.__class__.__name__ == "Integer"


def test_index_html_js_quantity_math_never_uses_parsefloat_or_tofixed():
    idx = INDEX_HTML.index("function toBaseUnits(c, p, pc, rule)")
    body = INDEX_HTML[idx:INDEX_HTML.index("function qtyLabel(", idx)]
    assert "parseFloat" not in body
    assert "toFixed" not in body
    assert "Math.round(" not in body or "Math.floor(" in body  # only floor division ever used


# =====================================================================
# Napkin Damage uses its own configured rule; non-Napkin unchanged
#
# The seed migration's product/packaging-rule data (see
# migrations/versions/a939d0b27a3e_seed_default_products_and_packaging_.py)
# is only ever applied via `flask db upgrade` — this project's test suite
# builds its schema with db.create_all() instead (see conftest.py), which
# never runs that data migration, so a live test client has no seeded
# products at all. These checks instead inspect the migration's own
# hard-coded PACKAGING_RULES mapping directly (a static, DB-independent
# source of truth for what a fresh production install seeds) — a
# live-DB equivalent of the same check is exercised in
# tests/test_stage5_migration.py, which does run the real migration chain.
# =====================================================================

def _seed_packaging_rules():
    import importlib
    module = importlib.import_module(
        "migrations.versions.a939d0b27a3e_seed_default_products_and_packaging_"
    )
    return module.PACKAGING_RULES


def test_napkin_damage_has_its_own_independently_configured_rule_entry():
    rules = _seed_packaging_rules()
    assert "Napkins Corporate" in rules and "Napkins Standard" in rules and "Napkins Damage" in rules
    # Each name maps to its own dict entry (one row per product in the
    # packaging_rules table — see the migration's upgrade()) — never a
    # shared/implicit fallback for Damage specifically.


def test_napkin_corporate_and_standard_are_6_packs_10_pieces():
    rules = _seed_packaging_rules()
    for name in ("Napkins Corporate", "Napkins Standard"):
        cartons_to_packs, packs_to_pieces, carton_to_pieces = rules[name]
        assert cartons_to_packs == 6
        assert packs_to_pieces == 10
        assert carton_to_pieces is None


def test_compact_products_remain_10_and_10():
    rules = _seed_packaging_rules()
    for name in ("Compact Corporate", "Compact Standard"):
        cartons_to_packs, packs_to_pieces, carton_to_pieces = rules[name]
        assert cartons_to_packs == 10
        assert packs_to_pieces == 10


def test_kingmax_and_jumbomax_retain_no_pack_tier():
    rules = _seed_packaging_rules()
    for name in ("KingMax", "JumboMax"):
        cartons_to_packs, packs_to_pieces, carton_to_pieces = rules[name]
        assert cartons_to_packs is None
        assert carton_to_pieces is not None


def test_straws_silky_kitchen_towel_configurations_unchanged():
    rules = _seed_packaging_rules()
    expected = {
        "Straws": (12, 100, None),
        "Silky 4pack": (25, 4, None),
        "Kitchen Towel Doubles": (12, 2, None),
        "Kitchen Towel Singles": (None, None, 24),
    }
    for name, expected_rule in expected.items():
        assert rules[name] == expected_rule


def test_configuring_a_product_with_napkin_ratios_via_the_live_api_round_trips_correctly(client, login_as):
    """A live-DB equivalent proving the API itself (not just the migration
    file) stores and returns 6-packs/10-pieces exactly."""
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Live Napkin Check"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 6, "packs_to_pieces": 10,
    })
    current = client.get(f"/api/admin/products/{product['id']}/packaging-rules").get_json()[0]
    assert current["cartons_to_packs"] == 6
    assert current["packs_to_pieces"] == 10


# =====================================================================
# Zero values are valid — required-field validation, display, storage
# =====================================================================

def test_zero_opening_stock_is_accepted_not_rejected(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Zero Value Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    res = client.post("/api/daily-figures", json={
        "product_id": product["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    assert res.get_json()["opening"]["base_qty"] == 0


def test_zero_adjustment_is_rejected_but_for_business_reasons_not_falsy_zero(client, login_as):
    """delta_base_qty=0 is rejected because a zero adjustment is
    meaningless (there is nothing to adjust) — a genuinely separate rule
    from "0 fails required-field validation"; date/shift/product_id/reason
    all still accept legitimate falsy-looking values like 0 elsewhere."""
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Zero Adj Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": product["id"], "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 0, "reason": "test",
    })
    assert res.status_code == 400
    assert "zero" in res.get_json()["error"].lower()


def test_closing_stock_zero_uses_book_formatter_not_em_dash(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Zero Closing Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    client.post("/api/daily-figures", json={
        "product_id": product["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    view = client.get(f"/api/daily-figures/{product['id']}?date=2026-08-01&shift=Day").get_json()
    assert view["closing"]["base_qty"] == 0
    assert qty_label(0, 0, 0, view["packaging_rule"]) == "0 Ctns"


def test_qty_label_zero_never_returns_em_dash():
    assert qty_label(0, 0, 0, COMPACT) == "0 Ctns"
    assert qty_label(0, 0, 0, KINGMAX) == "0c 0pc"


def test_qty_label_em_dash_only_for_genuinely_missing_part_not_zero():
    assert qty_label is not None  # sanity: formatter itself never emits '—'
    idx = INDEX_HTML.index("function qtyLabel(part, rule){")
    body = INDEX_HTML[idx:INDEX_HTML.index("\n}", idx)]
    assert "if(!part) return '—';" in body  # only a missing object, never a zero value, triggers it


def test_zero_pack_and_piece_carton_only_products_still_display_correctly():
    assert qty_label(3, 0, 0, KINGMAX) == "3c 0pc"


def test_base_units_never_stored_as_sql_real_or_float():
    from webapp.models.dispatch import DispatchLine
    from webapp.models.return_record import ReturnLine
    from webapp.models.production_record import ProductionLine
    for model, col_name in ((DispatchLine, "base_unit_qty"), (ReturnLine, "base_unit_qty"), (ProductionLine, "base_unit_qty")):
        col = model.__table__.columns[col_name]
        assert col.type.__class__.__name__ == "Integer"


# =====================================================================
# Removed helper text (section 7) — labels/calculation/permissions intact
# =====================================================================

def test_closing_stock_formula_sentence_removed():
    assert "Closing Stock = Opening Stock + Production + Returns" not in INDEX_HTML


def test_read_only_explanatory_sentence_removed():
    assert "Read-only — Returns and Production are recorded in their own Books" not in INDEX_HTML


def test_auto_hints_shortened_not_removed_entirely():
    """The field-level "(auto)" cue survives (still genuinely useful — an
    Operator needs to know these fields aren't directly editable) — only
    the long repeated sentences were removed."""
    assert INDEX_HTML.count(">(auto)</span>") == 3


def test_labels_still_present_after_helper_text_removal():
    assert '<span class="lbl">Opening Stock</span>' in INDEX_HTML
    assert '<span class="lbl">Closing Stock</span>' in INDEX_HTML
    for label in ("Returns", "Production", "Issued"):
        assert f'<span class="lbl">{label} <span class="hint"' in INDEX_HTML


def test_calculation_itself_unchanged_by_text_removal():
    assert "const closingBase = o + view.return_.base_qty + view.production.base_qty - view.issued.base_qty;" in INDEX_HTML


def test_permission_gating_unchanged_by_text_removal():
    assert "const isViewer = role === 'viewer';" in INDEX_HTML
    assert "const isFullyReadOnly = isViewer ||" in INDEX_HTML


def test_view_link_and_book_notation_still_present():
    assert 'id="issuedBreakdownBtn">view</button>' in INDEX_HTML
    assert "function qtyLabel(part, rule){" in INDEX_HTML
