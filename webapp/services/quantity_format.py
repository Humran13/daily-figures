"""
The one centralized formatter for turning an exact cartons/packs/pieces
triple into the same book-style notation the physical Dispatch/Returns/
Production books use — reused by every export route and (in equivalent
JS form) every page's read-only quantity display. Never duplicated,
never reimplemented per page/route.

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

    Products with NO pack tier (KingMax, JumboMax, Kitchen Towel Singles,
    etc.) keep their existing "Xc Ypc" convention unchanged — there is no
    Packs concept to position at all, and inventing one is exactly what
    this formatter must never do.
"""


def _has_pack_tier(rule):
    if rule is None:
        return False
    if isinstance(rule, dict):
        return rule.get("carton_to_pieces") is None
    return rule.has_pack_tier


def qty_label(cartons, packs, pieces, rule):
    """
    `rule` may be a PackagingRule instance, its to_dict() form, or None —
    call sites have whichever is already at hand (an ORM object when
    reading a *Line row directly, a plain dict when reading a
    stock_service.daily_figure_view() result).
    """
    if not _has_pack_tier(rule):
        return f"{cartons}c {pieces}pc"

    if packs == 0 and pieces == 0:
        return f"{cartons} Ctns"
    if 0 <= packs <= 9 and 0 <= pieces <= 9:
        return f"{cartons}.{packs}{pieces} Ctns"
    # Packs or Pieces don't fit a single positional digit for this
    # product's configuration — never display an invalid/ambiguous
    # remainder; fall back to the unambiguous explicit form instead.
    return f"{cartons}c {packs}p {pieces}pc"
