"""
Super-Administrator-only "Reset Daily Values" control (Stage 7 section 4).
Narrowly scoped to one Date + Shift, and either one Product or every
active product for that Date + Shift — never a wider blast radius than
that, and never touches another date.

What a reset clears: a manually-entered Opening Stock (reset to zero,
never deleted outright — the row itself, its id, and its created_at stay
intact, only the manual fields are cleared, so this is a correction-like
update rather than a destructive delete), Notes, and this stage's new
in-progress/completed review markers (webapp/services/daily_entry_status_service.py).

What a reset never touches: Issued (from finalized Dispatch), Returns
(from the finalized Returns Book), or Production (from the finalized
Production Book) — all three are computed live from their own source
books (see webapp/services/stock_service.py) and were never stored as
directly-resettable fields to begin with. If those figures are wrong, the
fix is to edit/void/reopen the source record, not this reset.
"""
from webapp.extensions import db
from webapp.models.daily_figure import OPENING_STOCK_SOURCE_DERIVED, DailyFigure
from webapp.models.dispatch import SHIFTS
from webapp.models.product import Product
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services import daily_entry_status_service
from webapp.services.audit_service import record_audit


class DailyResetError(ValueError):
    """User-facing validation problem — mapped to HTTP 400 by routes."""


def _require_super_admin(actor):
    if actor.role != ROLE_SUPER_ADMIN:
        raise DailyResetError("Only a Super Administrator may reset daily values")


def _target_products(product_id):
    if product_id is not None:
        product = db.session.get(Product, product_id)
        if product is None:
            raise DailyResetError("product does not exist")
        return [product]
    return Product.query.filter_by(active=True).order_by(Product.display_order, Product.name).all()


def _validate(date, shift, actor):
    _require_super_admin(actor)
    if not date:
        raise DailyResetError("date is required")
    if shift not in SHIFTS:
        raise DailyResetError(f"shift must be one of {SHIFTS}")


def preview(date, shift, product_id, actor):
    """What a reset would affect, without changing anything — shown to the
    Super Administrator before they confirm."""
    _validate(date, shift, actor)
    products = _target_products(product_id)

    rows = []
    for product in products:
        figure = DailyFigure.query.filter_by(product_id=product.id, date=date, shift=shift).first()
        status = daily_entry_status_service.status_view(date, shift, product.id)
        has_manual_opening = figure is not None and (
            figure.opening_cartons or figure.opening_packs or figure.opening_pieces or figure.opening_base_qty
        )
        rows.append({
            "product_id": product.id,
            "product_name": product.name,
            "has_manual_opening_entry": bool(has_manual_opening),
            "opening_qty": {
                "cartons": figure.opening_cartons, "packs": figure.opening_packs, "pieces": figure.opening_pieces,
            } if figure is not None else None,
            "is_opening_override": bool(figure and figure.opening_stock_is_override),
            "opening_stock_source": figure.opening_stock_source if figure is not None else None,
            "has_notes": bool(figure and figure.notes),
            "review_status": status["status"],
        })
    return {"date": date, "shift": shift, "products": rows}


def execute(date, shift, product_id, reason, actor):
    """Transactional: every affected product is cleared together, or (on
    any failure) none of them are — the caller's route commits once at the
    end and rolls back the whole session on any exception here."""
    _validate(date, shift, actor)
    if not reason:
        raise DailyResetError("A reason is required to reset daily values")
    products = _target_products(product_id)

    affected = []
    for product in products:
        figure = DailyFigure.query.filter_by(product_id=product.id, date=date, shift=shift).first()
        before = None
        if figure is not None:
            before = {
                "opening_cartons": figure.opening_cartons, "opening_packs": figure.opening_packs,
                "opening_pieces": figure.opening_pieces, "opening_base_qty": figure.opening_base_qty,
                "opening_stock_is_override": figure.opening_stock_is_override,
                "opening_stock_source": figure.opening_stock_source,
                "notes": figure.notes,
            }
            figure.opening_cartons = 0
            figure.opening_packs = 0
            figure.opening_pieces = 0
            figure.opening_base_qty = 0
            # The repair mechanism for a stuck/stale row (Stage 8
            # correction): clearing the anchor classification, not just
            # the quantity, means carry-forward stops trusting this row at
            # all and goes back to deriving this period live from whatever
            # real anchor precedes it. This is the one place besides an
            # explicit correction write that may retire a manual_correction
            # too — a Super Administrator reset is itself a deliberate,
            # audited override of whatever was there before.
            figure.opening_stock_source = OPENING_STOCK_SOURCE_DERIVED
            figure.opening_stock_is_override = False
            figure.notes = None
            figure.updated_by = actor.id

        daily_entry_status_service.clear_for_reset(date, shift, product.id, actor, reason)

        if before is not None:
            affected.append({"product_id": product.id, "product_name": product.name, "before": before})
        else:
            affected.append({"product_id": product.id, "product_name": product.name, "before": None})

    db.session.flush()
    record_audit(
        actor, "reset_daily_values", "daily_figure_reset",
        entity_id=f"{date}|{shift}|{product_id if product_id is not None else 'all'}",
        before={"products": [a["product_id"] for a in affected]},
        after={"reason": reason, "product_count": len(affected)},
    )
    return {"date": date, "shift": shift, "products": [a["product_name"] for a in affected]}
