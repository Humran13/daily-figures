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
// everything the server renders.
function fromBaseUnitsPreview(total, rule){
  if(!rule) return {cartons: total, packs: 0, pieces: 0};
  if(hasPackTier(rule)){
    const totalPacks = Math.floor(total / rule.packs_to_pieces);
    const pieces = total % rule.packs_to_pieces;
    const cartons = Math.floor(totalPacks / rule.cartons_to_packs);
    const packs = totalPacks % rule.cartons_to_packs;
    return {cartons, packs, pieces};
  }
  return {cartons: Math.floor(total / rule.carton_to_pieces), packs: 0, pieces: total % rule.carton_to_pieces};
}

function qtyLabel(part, rule){
  if(!part) return '—';
  if(part.warning) return part.warning;
  if(part.cartons === undefined) return String(part.base_qty);
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
