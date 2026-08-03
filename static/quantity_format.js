// The one centralized formatter for turning an exact cartons/packs/pieces
// triple into the same book-style notation the physical Dispatch/Returns/
// Production books use — mirrors webapp/services/quantity_format.py's
// qty_label() exactly (same positional "C.PP Ctns" notation for both
// pack-tier and no-pack-tier products, same single-digit-positional-slot
// guard, same fallback for a product with no rule at all). Loaded before
// every page's own inline <script> (see index.html/history.html) so it is
// defined once, shared, and never duplicated per page.
function hasPackTier(rule){ return rule && rule.carton_to_pieces === null; }

function toBaseUnits(c, p, pc, rule){
  if(!rule) return 0;
  if(hasPackTier(rule)) return (c*rule.cartons_to_packs + p) * rule.packs_to_pieces + pc;
  return c*rule.carton_to_pieces + pc;
}

// Pure integer split — mirrors webapp/services/packaging.py's from_base_units()
// exactly, so a live pre-save preview uses the same book notation as
// everything the server renders. Final legacy-migration investigation,
// section 9 — negative `total` is split on its ABSOLUTE value (JS's `%`
// is truncating, not floored, so applying it directly to a negative
// dividend disagrees with Math.floor()-based cartons; splitting the
// absolute value sidesteps that entirely) with the sign carried on the
// most-significant nonzero component — mirrors stock_service._split_or_none()
// exactly. A magnitude smaller than one whole carton splits to cartons=0,
// and `-0 === 0` in JS too, so forcing the sign onto cartons regardless
// would silently lose it — the sign falls onto `packs` (or `pieces`, if
// packs is also 0) instead. At most one of the three is ever negative.
function fromBaseUnitsPreview(total, rule){
  if(!rule) return {cartons: total, packs: 0, pieces: 0};
  const negative = total < 0;
  const abs = Math.abs(total);
  let cartons, packs, pieces;
  if(hasPackTier(rule)){
    const totalPacks = Math.floor(abs / rule.packs_to_pieces);
    pieces = abs % rule.packs_to_pieces;
    cartons = Math.floor(totalPacks / rule.cartons_to_packs);
    packs = totalPacks % rule.cartons_to_packs;
  } else {
    cartons = Math.floor(abs / rule.carton_to_pieces);
    packs = 0;
    pieces = abs % rule.carton_to_pieces;
  }
  if(negative){
    if(cartons !== 0) cartons = -cartons;
    else if(packs !== 0) packs = -packs;
    else pieces = -pieces;
  }
  return {cartons, packs, pieces};
}

// Final legacy-migration investigation, section 9 — a negative quantity
// (a genuine negative Closing/Opening Stock balance) is fully expressible
// in book notation: the sign lives on whichever component
// fromBaseUnitsPreview()/stock_service._split_or_none() put it on —
// normally `cartons`, but on the backend for a sub-carton magnitude,
// `packs` or `pieces` instead (see those functions), formatted as one
// leading minus sign on the complete label — "-6.00 Ctns" / "-0.50 Ctns",
// never "-600 Ctns" (a raw base-unit number mislabeled as cartons) and
// never a text warning replacing the number. Mirrors webapp/services/
// quantity_format.py's qty_label() exactly, including the negative
// pack-tier case always showing both digits ("-7.00 Ctns", not "-7 Ctns")
// even though the positive case drops a zero remainder.
function qtyLabel(part, rule){
  if(!part) return '—';
  if(part.cartons === undefined) return String(part.base_qty);
  if(part.cartons < 0 || part.packs < 0 || part.pieces < 0){
    const absPart = {cartons: Math.abs(part.cartons), packs: Math.abs(part.packs), pieces: Math.abs(part.pieces)};
    if(!hasPackTier(rule)) return `-${qtyLabel(absPart, rule)}`;
    if(absPart.packs <= 9 && absPart.pieces <= 9) return `-${absPart.cartons}.${absPart.packs}${absPart.pieces} Ctns`;
    return `-${absPart.cartons}c ${absPart.packs}p ${absPart.pieces}pc`;
  }
  if(!hasPackTier(rule)){
    const cartonToPieces = rule && rule.carton_to_pieces;
    if(!cartonToPieces) return `${part.cartons}c ${part.pieces}pc`;
    const width = Math.max(2, String(cartonToPieces - 1).length);
    return `${part.cartons}.${String(part.pieces).padStart(width, '0')} Ctns`;
  }
  if(part.packs === 0 && part.pieces === 0) return `${part.cartons} Ctns`;
  if(part.packs <= 9 && part.pieces <= 9) return `${part.cartons}.${part.packs}${part.pieces} Ctns`;
  // Packs or Pieces don't fit a single positional digit for this product's
  // configuration — never display an invalid/ambiguous remainder.
  return `${part.cartons}c ${part.packs}p ${part.pieces}pc`;
}
