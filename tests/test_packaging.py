from types import SimpleNamespace

import pytest

from webapp.services.packaging import (
    PackagingError,
    from_base_units,
    normalize,
    to_base_units,
)


def rule(cartons_to_packs=None, packs_to_pieces=None, carton_to_pieces=None):
    r = SimpleNamespace(
        cartons_to_packs=cartons_to_packs,
        packs_to_pieces=packs_to_pieces,
        carton_to_pieces=carton_to_pieces,
    )
    r.has_pack_tier = cartons_to_packs is not None
    return r


# Group A: 1 carton = 10 packs, 1 pack = 10 pieces
GROUP_A = rule(cartons_to_packs=10, packs_to_pieces=10)
# Napkins: 1 carton = 6 packs, 1 pack = 10 pieces
NAPKINS = rule(cartons_to_packs=6, packs_to_pieces=10)
# KingMax: 1 carton = 60 pieces, no pack tier
KINGMAX = rule(carton_to_pieces=60)
# Straws: 1 carton = 12 packs, 1 pack = 100 pieces
STRAWS = rule(cartons_to_packs=12, packs_to_pieces=100)


def test_spec_example_two_cartons_three_packs_four_pieces():
    # spec's worked example: 2 cartons, 3 packs, 4 pieces = 234 total pieces
    assert to_base_units(2, 3, 4, GROUP_A) == 234


def test_group_a_whole_carton_only():
    assert to_base_units(1, 0, 0, GROUP_A) == 100


def test_napkins_ratio_differs_from_group_a():
    assert to_base_units(1, 0, 0, NAPKINS) == 60
    assert to_base_units(0, 1, 0, NAPKINS) == 10


def test_kingmax_no_pack_tier():
    assert to_base_units(2, 0, 30, KINGMAX) == 150


def test_kingmax_rejects_nonzero_packs():
    with pytest.raises(PackagingError):
        to_base_units(1, 1, 0, KINGMAX)


def test_straws_carton_ratio():
    assert to_base_units(1, 0, 0, STRAWS) == 1200


def test_negative_quantities_rejected():
    with pytest.raises(PackagingError):
        to_base_units(-1, 0, 0, GROUP_A)


def test_normalize_rolls_up_excess_pieces_and_packs():
    # 24 pieces -> +2 packs, 4 pieces left; 13+2=15 packs -> +1 carton, 5 packs left
    cartons, packs, pieces = normalize(0, 13, 24, GROUP_A)
    assert (cartons, packs, pieces) == (1, 5, 4)
    # total pieces must be identical before and after normalizing
    assert to_base_units(0, 13, 24, GROUP_A) == to_base_units(cartons, packs, pieces, GROUP_A)


def test_normalize_no_pack_tier_rolls_pieces_into_cartons():
    cartons, packs, pieces = normalize(0, 0, 125, KINGMAX)
    assert (cartons, packs, pieces) == (2, 0, 5)


def test_from_base_units_round_trips_with_to_base_units():
    for total in [0, 1, 9, 10, 99, 100, 234, 1199, 1200]:
        cartons, packs, pieces = from_base_units(total, GROUP_A)
        assert to_base_units(cartons, packs, pieces, GROUP_A) == total


def test_from_base_units_no_pack_tier():
    cartons, packs, pieces = from_base_units(150, KINGMAX)
    assert (cartons, packs, pieces) == (2, 0, 30)
