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
    """
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
