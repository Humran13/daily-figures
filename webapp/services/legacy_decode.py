"""
Decodes the OLD Daily Figures entries.{opening,return_val,production} REAL
values into exact cartons/packs/pieces.

Per direct clarification: these were never true decimal cartons. They are a
compact "C.PP" notation — the integer part is cartons, and the two decimal
digits are read LITERALLY as a packs digit and a pieces digit (not as a
fraction of a carton). E.g. for Compact (10 packs/carton, 10 pieces/pack),
1.24 means 1 carton + 2 packs + 4 pieces = 124 pieces. For Napkins (6
packs/carton, 10 pieces/pack), 1.24 means 1 carton + 2 packs + 4 pieces =
84 pieces — NOT 1.24 * 60.

This notation can only faithfully represent a product whose packs-per-carton
and pieces-per-pack (or pieces-per-carton, for no-pack-tier products) each
fit in a single decimal digit's range (0-9) or two digits (0-99) for the
no-pack-tier case. Any product whose ratio exceeds that (Straws: 12 packs
and 100 pieces/pack; Silky 4-pack: 25 packs; Kitchen Towel Doubles: 12
packs) cannot be reliably reverse-engineered from the number alone — those,
and any individual value that decodes to an out-of-range digit, must be
flagged for manual review rather than guessed.
"""


class AmbiguousLegacyValue(ValueError):
    pass


def ratio_is_decodable(rule):
    """Whether this product's packaging ratio even fits the old single/double-digit notation."""
    if rule.has_pack_tier:
        return rule.cartons_to_packs <= 10 and rule.packs_to_pieces <= 10
    return rule.carton_to_pieces <= 99


def decode_legacy_value(value, rule):
    """
    Returns (cartons, packs, pieces) or raises AmbiguousLegacyValue with a
    human-readable reason. Never guesses past an out-of-range or
    structurally-undecodable case.
    """
    if value is None:
        raise AmbiguousLegacyValue("value is missing")
    if value < 0:
        raise AmbiguousLegacyValue(f"negative value ({value}) cannot be decoded")

    if not ratio_is_decodable(rule):
        if rule.has_pack_tier:
            raise AmbiguousLegacyValue(
                f"this product's ratio (1 carton = {rule.cartons_to_packs} packs, "
                f"1 pack = {rule.packs_to_pieces} pieces) exceeds what the old single-digit "
                "notation could represent — cannot be reverse-engineered from the number alone"
            )
        raise AmbiguousLegacyValue(
            f"this product's ratio (1 carton = {rule.carton_to_pieces} pieces) exceeds what the "
            "old two-digit notation could represent — cannot be reverse-engineered from the number alone"
        )

    cartons = int(value)
    frac_cents = round((value - cartons) * 100)
    if frac_cents >= 100:  # floating-point edge case, e.g. 2.999999997
        cartons += 1
        frac_cents = 0

    if rule.has_pack_tier:
        packs, pieces = divmod(frac_cents, 10)
        if packs >= rule.cartons_to_packs or pieces >= rule.packs_to_pieces:
            raise AmbiguousLegacyValue(
                f"decoded to {packs} packs / {pieces} pieces, out of range for this product "
                f"(max {rule.cartons_to_packs - 1} packs, max {rule.packs_to_pieces - 1} pieces) — "
                f"raw value was {value}"
            )
        return cartons, packs, pieces

    pieces = frac_cents
    if pieces >= rule.carton_to_pieces:
        raise AmbiguousLegacyValue(
            f"decoded to {pieces} pieces, out of range for this product "
            f"(max {rule.carton_to_pieces - 1}) — raw value was {value}"
        )
    return cartons, 0, pieces
