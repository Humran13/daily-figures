"""
Final pre-live correction: the mandatory book-style positional quantity
notation. webapp/services/quantity_format.qty_label() is the one
centralized formatter — reused by every export route (see
tests/test_returns.py, tests/test_production.py, tests/test_exports_routes.py,
tests/test_stage5_exports_and_reports.py for the export-level checks) and
mirrored in JS on every page that displays a read-only quantity.

Book notation for a product WITH a pack tier: "C.PP Ctns", where the two
digits after the decimal point are POSITIONAL — first digit is Packs,
second is Pieces — never a base-10 fraction. Products WITHOUT a pack tier
keep their existing "Xc Ypc" convention unchanged.
"""
import inspect

from webapp.services.quantity_format import qty_label

GROUP_A_RULE = {"cartons_to_packs": 10, "packs_to_pieces": 10, "carton_to_pieces": None}
NAPKIN_RULE = {"cartons_to_packs": 6, "packs_to_pieces": 10, "carton_to_pieces": None}
KINGMAX_RULE = {"cartons_to_packs": None, "packs_to_pieces": None, "carton_to_pieces": 60}
JUMBOMAX_RULE = {"cartons_to_packs": None, "packs_to_pieces": None, "carton_to_pieces": 24}
STRAWS_RULE = {"cartons_to_packs": 12, "packs_to_pieces": 100, "carton_to_pieces": None}


# ---------- exact examples from the spec ----------

def test_five_cartons_zero_packs_zero_pieces():
    assert qty_label(5, 0, 0, GROUP_A_RULE) == "5 Ctns"


def test_one_carton_one_pack_one_piece():
    assert qty_label(1, 1, 1, GROUP_A_RULE) == "1.11 Ctns"


def test_five_cartons_six_packs_eight_pieces():
    assert qty_label(5, 6, 8, GROUP_A_RULE) == "5.68 Ctns"


def test_six_cartons_one_pack_one_piece():
    assert qty_label(6, 1, 1, GROUP_A_RULE) == "6.11 Ctns"


# ---------- not decimal arithmetic ----------

def test_notation_is_not_reinterpreted_as_a_decimal_fraction():
    # 5 cartons 6 packs 8 pieces must render "5.68", never anything derived
    # from treating 6 and 8 as a base-10 fraction of a carton.
    label = qty_label(5, 6, 8, GROUP_A_RULE)
    assert label == "5.68 Ctns"
    assert "5.68000" not in label  # no floating-point artifact of any kind


def test_formatter_never_uses_floating_point():
    source = inspect.getsource(qty_label)
    assert "float(" not in source
    assert "round(" not in source
    assert " / " not in source and "//" not in source  # no division at all — pure string formatting of integers


# ---------- trailing/leading zero positional digits ----------

def test_zero_packs_nonzero_pieces_still_shows_both_digit_positions():
    assert qty_label(5, 0, 3, GROUP_A_RULE) == "5.03 Ctns"


def test_nonzero_packs_zero_pieces_still_shows_both_digit_positions():
    assert qty_label(5, 6, 0, GROUP_A_RULE) == "5.60 Ctns"


# ---------- Napkin's 6-packs-per-carton rule ----------

def test_napkin_six_packs_per_carton_rule_uses_same_positional_notation():
    assert qty_label(1, 2, 4, NAPKIN_RULE) == "1.24 Ctns"


def test_napkin_max_pack_value_five_still_single_digit():
    # cartons_to_packs=6 means a normalized Napkin packs value never
    # exceeds 5 — always representable positionally.
    assert qty_label(2, 5, 9, NAPKIN_RULE) == "2.59 Ctns"


# ---------- no-pack-tier products keep their existing rule, never a fake pack ----------

def test_kingmax_never_receives_a_fake_pack_tier():
    assert qty_label(2, 0, 10, KINGMAX_RULE) == "2c 10pc"
    assert "Ctns" not in qty_label(2, 0, 10, KINGMAX_RULE)
    assert "." not in qty_label(2, 0, 10, KINGMAX_RULE)


def test_jumbomax_never_receives_a_fake_pack_tier():
    assert qty_label(3, 0, 1, JUMBOMAX_RULE) == "3c 1pc"
    assert "Ctns" not in qty_label(3, 0, 1, JUMBOMAX_RULE)


def test_no_rule_configured_falls_back_safely():
    assert qty_label(1, 0, 0, None) == "1c 0pc"


# ---------- guard against an invalid/ambiguous positional remainder ----------

def test_pack_value_exceeding_single_digit_falls_back_instead_of_corrupting_notation():
    """Straws (12 packs/carton, 100 pieces/pack) can have a two-digit packs
    value (e.g. 11) — the positional slot can't represent that, so this
    must fall back to the explicit form rather than emit something like
    "5.115" that looks like valid book notation but isn't."""
    label = qty_label(5, 11, 45, STRAWS_RULE)
    assert label == "5c 11p 45pc"
    assert "Ctns" not in label


def test_pieces_value_exceeding_single_digit_falls_back_too():
    label = qty_label(2, 3, 45, STRAWS_RULE)
    assert label == "2c 3p 45pc"


def test_straws_still_uses_positional_notation_when_actual_values_fit():
    """A specific Straws entry whose actual packs/pieces both happen to be
    single-digit is still representable — the fallback is per-value, not a
    blanket exclusion of the whole product."""
    assert qty_label(1, 3, 7, STRAWS_RULE) == "1.37 Ctns"


# ---------- consistent across all five Daily Figures fields ----------

def test_same_formatter_produces_consistent_output_for_every_daily_figures_field():
    values = {"opening": (5, 0, 0), "production": (5, 0, 0), "return_": (1, 1, 1),
              "issued": (0, 0, 0), "closing": (6, 1, 1)}
    labels = {k: qty_label(*v, GROUP_A_RULE) for k, v in values.items()}
    assert labels["opening"] == "5 Ctns"
    assert labels["production"] == "5 Ctns"
    assert labels["return_"] == "1.11 Ctns"
    assert labels["issued"] == "0 Ctns"
    assert labels["closing"] == "6.11 Ctns"
