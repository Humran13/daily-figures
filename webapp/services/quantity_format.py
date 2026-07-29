"""
The one place exports format a cartons/packs/pieces triple into the same
business-friendly string the frontend already shows on screen (see
index.html's qtyLabel()/history.html's qtyLabel() — "3c 2p 5pc" for a
product with a pack tier, "3c 5pc" for one without). Exports must speak
the same packaging language as the rest of the app rather than inventing
a second "Total Pieces" column as the primary figure — see the Stage 5
spec's report-language section. This never recomputes or reinterprets a
quantity; it only formats numbers webapp/services/packaging.py already
produced.
"""


def qty_label(cartons, packs, pieces, rule):
    """
    `rule` may be a PackagingRule instance, its to_dict() form, or None —
    call sites in export routes have whichever is already at hand (an ORM
    object when reading DispatchLine/ReturnLine/ProductionLine directly, a
    plain dict when reading a stock_service.daily_figure_view() result).
    Either way, the same has_pack_tier/carton_to_pieces distinction
    packaging.py and the frontend already use decides whether a Packs
    segment applies at all.
    """
    if rule is None:
        has_pack_tier = False
    elif isinstance(rule, dict):
        has_pack_tier = rule.get("carton_to_pieces") is None
    else:
        has_pack_tier = rule.has_pack_tier
    if has_pack_tier:
        return f"{cartons}c {packs}p {pieces}pc"
    return f"{cartons}c {pieces}pc"
