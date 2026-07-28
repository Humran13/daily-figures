"""
Cartons/packs/pieces conversion. Pure integer arithmetic only — quantities
must never touch floating point, since a rounding error here is a wrong
stock count in the warehouse.
"""


class PackagingError(ValueError):
    pass


def normalize(cartons, packs, pieces, rule):
    """
    Roll excess packs/pieces up into the next unit, e.g. 13 packs at a
    10-packs-per-carton ratio becomes +1 carton, 3 packs. Never discards a
    pack or a piece — everything is redistributed, never dropped.
    Returns (cartons, packs, pieces) as a new normalized tuple.
    """
    cartons, packs, pieces = int(cartons or 0), int(packs or 0), int(pieces or 0)
    if cartons < 0 or packs < 0 or pieces < 0:
        raise PackagingError("Cartons, packs, and pieces cannot be negative")

    if rule.has_pack_tier:
        if pieces >= rule.packs_to_pieces:
            packs += pieces // rule.packs_to_pieces
            pieces = pieces % rule.packs_to_pieces
        if packs >= rule.cartons_to_packs:
            cartons += packs // rule.cartons_to_packs
            packs = packs % rule.cartons_to_packs
    else:
        if packs != 0:
            raise PackagingError("This product has no pack unit — packs must be 0")
        if pieces >= rule.carton_to_pieces:
            cartons += pieces // rule.carton_to_pieces
            pieces = pieces % rule.carton_to_pieces

    return cartons, packs, pieces


def to_base_units(cartons, packs, pieces, rule):
    """Convert an (already normalized or raw) cartons/packs/pieces triple to a total integer piece count."""
    cartons, packs, pieces = int(cartons or 0), int(packs or 0), int(pieces or 0)
    if cartons < 0 or packs < 0 or pieces < 0:
        raise PackagingError("Cartons, packs, and pieces cannot be negative")

    if rule.has_pack_tier:
        return (cartons * rule.cartons_to_packs + packs) * rule.packs_to_pieces + pieces

    if packs != 0:
        raise PackagingError("This product has no pack unit — packs must be 0")
    return cartons * rule.carton_to_pieces + pieces


def from_base_units(total_pieces, rule):
    """Split a total integer piece count back into a display cartons/packs/pieces triple."""
    total_pieces = int(total_pieces or 0)
    if total_pieces < 0:
        raise PackagingError("Total pieces cannot be negative")

    if rule.has_pack_tier:
        total_packs, pieces = divmod(total_pieces, rule.packs_to_pieces)
        cartons, packs = divmod(total_packs, rule.cartons_to_packs)
        return cartons, packs, pieces

    cartons, pieces = divmod(total_pieces, rule.carton_to_pieces)
    return cartons, 0, pieces
