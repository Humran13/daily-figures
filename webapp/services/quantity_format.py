"""
The one centralized formatter for turning an exact cartons/packs/pieces
triple into the same book-style notation the physical Dispatch/Returns/
Production books use — reused by every export route and (in equivalent
JS form, see static/quantity_format.js) every page's read-only quantity
display. Never duplicated, never reimplemented per page/route.

Book notation (for a product WITH a pack tier):
    C.PP Ctns — the digits after the decimal point are POSITIONAL: the
    first is Packs, the second is Pieces. This is not decimal arithmetic.
    5.68 Ctns means exactly 5 Cartons, 6 Packs, 8 Pieces — never
    "five point six eight" cartons, and never recomputed as a base-10
    fraction. When both Packs and Pieces are zero, the decimal portion is
    dropped entirely ("5 Ctns", not "5.00 Ctns").

    This positional trick only has an unambiguous single-digit slot for
    Packs and for Pieces — so it is only used when the actual values in
    hand are each 0-9. Packaging configs are already normalized
    (packaging.py's normalize()/from_base_units() never return an
    out-of-range packs/pieces for the product's own rule), so this covers
    every product whose configuration keeps both within a single digit
    (e.g. Compact/Lavex/Premium/Mambo/Silky-10-pack's 10-and-10, Napkins'
    6-and-10). For a product where Packs or Pieces could exceed 9 (e.g.
    Straws' 12-and-100), the positional slot can't represent the value
    without ambiguity, so this falls back to the plain "Xc Yp Zpc" form
    instead of displaying something invalid or misleading.

Book notation (for a product with NO pack tier — cartons + loose pieces
directly, e.g. KingMax, JumboMax, Kitchen Towel Singles):
    C.PP Ctns — again purely POSITIONAL, not decimal arithmetic: the
    portion after the point is the loose-piece remainder (0 up to that
    product's own carton capacity minus one), zero-padded to at least two
    digits so it always occupies a fixed width. 5 Cartons + 3 loose
    pieces is "5.03 Ctns", never "5.3 Ctns" and never treated as "5.3"
    cartons. Unlike the pack-tier case, this is never dropped for a zero
    remainder ("0.00 Ctns", not "0 Ctns") — there is only one positional
    slot here, so there's no ambiguity to avoid by dropping it, and a
    fixed two-digit shape reads unambiguously as book notation rather
    than a plain integer count.

    The remainder's digit width is derived from the product's own
    packaging rule (max remainder = carton_to_pieces - 1), never
    hard-coded per product name — KingMax (60 pieces/carton, remainder
    0-59) and JumboMax (24 pieces/carton, remainder 0-23) both happen to
    need exactly two digits; a future product needing three would get
    three automatically. The caller must always pass an already-
    normalized (cartons, pieces) pair (see packaging.py's
    normalize()/from_base_units() — pieces is never allowed to reach or
    exceed carton_to_pieces going into this formatter, exactly as the
    pack-tier case already assumes for packs/pieces).
"""


def _has_pack_tier(rule):
    if rule is None:
        return False
    if isinstance(rule, dict):
        return rule.get("carton_to_pieces") is None
    return rule.has_pack_tier


def _carton_to_pieces(rule):
    if rule is None:
        return None
    if isinstance(rule, dict):
        return rule.get("carton_to_pieces")
    return rule.carton_to_pieces


def qty_label(cartons, packs, pieces, rule):
    """
    `rule` may be a PackagingRule instance, its to_dict() form, or None —
    call sites have whichever is already at hand (an ORM object when
    reading a *Line row directly, a plain dict when reading a
    stock_service.daily_figure_view() result).

    Final legacy-migration investigation, section 9 — a negative quantity
    (a genuine negative Closing/Opening Stock balance) is fully
    expressible in book notation: the sign lives on whichever component
    stock_service._split_or_none() put it on — normally `cartons`, but for
    a sub-carton magnitude (e.g. -50 base units on a 100-per-carton rule,
    which splits to 0 cartons) `cartons` would be `-0 == 0` and silently
    lose the sign entirely, so the split instead negates `packs` (or
    `pieces`, if both cartons and packs are 0) in that case — never more
    than one component is negative at once. Formatted here as one leading
    minus sign on the complete label — "-6.00 Ctns" or "-0.50 Ctns", never
    "-600 Ctns" (a raw base-unit number mislabeled as cartons) and never a
    text warning replacing the number. Unlike the positive pack-tier case
    below (which drops a zero ".00" remainder — "5 Ctns", not "5.00
    Ctns"), a negative pack-tier quantity always shows both digits, e.g.
    "-7.00 Ctns" rather than "-7 Ctns" — a deliberate, slightly more
    explicit shape that marks a negative balance as worth a second look,
    per the reported examples this fixes.
    """
    if cartons < 0 or packs < 0 or pieces < 0:
        abs_cartons, abs_packs, abs_pieces = abs(cartons), abs(packs), abs(pieces)
        if not _has_pack_tier(rule):
            return f"-{qty_label(abs_cartons, abs_packs, abs_pieces, rule)}"
        if 0 <= abs_packs <= 9 and 0 <= abs_pieces <= 9:
            return f"-{abs_cartons}.{abs_packs}{abs_pieces} Ctns"
        return f"-{abs_cartons}c {abs_packs}p {abs_pieces}pc"
    if not _has_pack_tier(rule):
        carton_to_pieces = _carton_to_pieces(rule)
        if not carton_to_pieces:
            # No rule (or a malformed one) to derive a remainder width
            # from — fall back rather than guessing, so a missing rule
            # never silently produces a misleading "book" notation.
            return f"{cartons}c {pieces}pc"
        width = max(2, len(str(carton_to_pieces - 1)))
        return f"{cartons}.{pieces:0{width}d} Ctns"

    if packs == 0 and pieces == 0:
        return f"{cartons} Ctns"
    if 0 <= packs <= 9 and 0 <= pieces <= 9:
        return f"{cartons}.{packs}{pieces} Ctns"
    # Packs or Pieces don't fit a single positional digit for this
    # product's configuration — never display an invalid/ambiguous
    # remainder; fall back to the unambiguous explicit form instead.
    return f"{cartons}c {packs}p {pieces}pc"
