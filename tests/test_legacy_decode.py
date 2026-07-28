from types import SimpleNamespace

import pytest

from webapp.services.legacy_decode import AmbiguousLegacyValue, decode_legacy_value, ratio_is_decodable


def rule(cartons_to_packs=None, packs_to_pieces=None, carton_to_pieces=None):
    r = SimpleNamespace(
        cartons_to_packs=cartons_to_packs,
        packs_to_pieces=packs_to_pieces,
        carton_to_pieces=carton_to_pieces,
    )
    r.has_pack_tier = cartons_to_packs is not None
    return r


GROUP_A = rule(cartons_to_packs=10, packs_to_pieces=10)     # Compact/Lavex/Premium/Mambo/Silky10
NAPKINS = rule(cartons_to_packs=6, packs_to_pieces=10)
KINGMAX = rule(carton_to_pieces=60)
STRAWS = rule(cartons_to_packs=12, packs_to_pieces=100)     # exceeds single-digit notation
SILKY4 = rule(cartons_to_packs=25, packs_to_pieces=4)       # exceeds single-digit notation


def test_group_a_decodes_digit_for_digit():
    # user's own worked example: 1.24 = 1 carton + 2 packs + 4 pieces
    assert decode_legacy_value(1.24, GROUP_A) == (1, 2, 4)


def test_napkins_same_digits_different_meaning():
    # same "1.24" text, but reads as 1 carton + 2 packs + 4 pieces for
    # Napkins too — the DIGITS are literal, not a fraction of the carton
    assert decode_legacy_value(1.24, NAPKINS) == (1, 2, 4)


def test_zero_decodes_to_zero():
    assert decode_legacy_value(0, GROUP_A) == (0, 0, 0)


def test_whole_carton_no_fraction():
    assert decode_legacy_value(2.0, GROUP_A) == (2, 0, 0)


def test_napkins_overflow_packs_digit_flagged():
    # Napkins only has 6 packs/carton (digits 0-5 valid) — digit 7 overflows
    with pytest.raises(AmbiguousLegacyValue):
        decode_legacy_value(1.74, NAPKINS)


def test_group_a_pieces_digit_never_overflows():
    # Group A allows pieces digit up to 9 (packs_to_pieces=10) — always valid
    assert decode_legacy_value(0.09, GROUP_A) == (0, 0, 9)


def test_kingmax_no_pack_tier_uses_two_digit_piece_count():
    assert decode_legacy_value(1.24, KINGMAX) == (1, 0, 24)


def test_kingmax_overflow_pieces_flagged():
    # KingMax carton = 60 pieces — 65 is out of range
    with pytest.raises(AmbiguousLegacyValue):
        decode_legacy_value(1.65, KINGMAX)


def test_negative_value_flagged():
    with pytest.raises(AmbiguousLegacyValue):
        decode_legacy_value(-1.0, GROUP_A)


def test_floating_point_rollover_edge_case():
    # 2.999999997 should round to 3 cartons flat, not crash or misdecode
    cartons, packs, pieces = decode_legacy_value(2.999999997, GROUP_A)
    assert (cartons, packs, pieces) == (3, 0, 0)


def test_straws_ratio_not_decodable_at_all():
    assert ratio_is_decodable(STRAWS) is False
    with pytest.raises(AmbiguousLegacyValue):
        decode_legacy_value(1.0, STRAWS)


def test_silky4pack_ratio_not_decodable_at_all():
    assert ratio_is_decodable(SILKY4) is False
    with pytest.raises(AmbiguousLegacyValue):
        decode_legacy_value(0.0, SILKY4)


def test_group_a_and_kingmax_ratios_are_decodable():
    assert ratio_is_decodable(GROUP_A) is True
    assert ratio_is_decodable(KINGMAX) is True
