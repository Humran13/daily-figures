"""
Production Book business rules — packaging conversion at entry time and
the draft/finalized/void lifecycle. Mirrors
webapp/services/dispatch_service.py: same build_line()/normalize()/
to_base_units() reuse, same finalize/reopen/void shape. Simpler than
Dispatch/Returns — no recipient, no invoice/dispatch number — but `shift`
is mandatory (Day or Night), unlike Returns which has none.
"""
from datetime import datetime, timezone

from webapp.extensions import db
from webapp.models.dispatch import SHIFTS
from webapp.models.product import Product
from webapp.models.production_record import (
    STATUS_DRAFT, STATUS_FINALIZED, STATUS_VOID, ProductionLine, ProductionRecord,
)
from webapp.services.packaging import PackagingError, normalize, to_base_units


class ProductionError(ValueError):
    """Raised for user-facing validation problems (mapped to HTTP 400/409 by routes)."""


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def can_edit(production_record, user):
    from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
    if user.role in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        return True
    return production_record.status == STATUS_DRAFT and production_record.created_by == user.id


def build_line(product_id, cartons, packs, pieces, line_notes=None, at=None):
    """Identical validation/conversion contract to dispatch_service.build_line()."""
    product = db.session.get(Product, product_id)
    if product is None or not product.active:
        raise ProductionError(f"Product {product_id} does not exist or is inactive")

    rule = product.current_packaging_rule(at=at)
    if rule is None:
        raise ProductionError(
            f"'{product.name}' has no packaging rule configured yet — "
            "set one in Admin > Products before recording production for it"
        )

    try:
        cartons, packs, pieces = normalize(cartons, packs, pieces, rule)
        base_units = to_base_units(cartons, packs, pieces, rule)
    except PackagingError as e:
        raise ProductionError(str(e)) from e

    if base_units == 0:
        raise ProductionError(f"'{product.name}' line has zero quantity")

    return {
        "product_id": product.id,
        "cartons": cartons,
        "packs": packs,
        "pieces": pieces,
        "base_unit_qty": base_units,
        "packaging_rule_id": rule.id,
        "line_notes": line_notes,
    }


def create_production(*, date, shift, remarks, lines, user):
    if not lines:
        raise ProductionError("A production entry needs at least one product line")
    if shift not in SHIFTS:
        raise ProductionError(f"shift must be one of {SHIFTS}")

    now = _utcnow()
    built_lines = [build_line(**line, at=now) for line in lines]

    record = ProductionRecord(
        date=date, shift=shift, remarks=remarks, status=STATUS_DRAFT,
        created_by=user.id, updated_by=user.id,
    )
    db.session.add(record)
    db.session.flush()

    for line in built_lines:
        db.session.add(ProductionLine(production_id=record.id, **line))
    db.session.flush()

    return record


def update_header(production_record, *, date=None, shift=None, remarks=None, user):
    if production_record.status != STATUS_DRAFT:
        raise ProductionError("only a draft production entry's header can be edited directly — reopen it first")

    if date is not None:
        production_record.date = date
    if shift is not None:
        if shift not in SHIFTS:
            raise ProductionError(f"shift must be one of {SHIFTS}")
        production_record.shift = shift
    if remarks is not None:
        production_record.remarks = remarks
    production_record.updated_by = user.id
    return production_record


def finalize_production(production_record, user):
    if production_record.status != STATUS_DRAFT:
        raise ProductionError(f"Only a draft production entry can be finalized (this one is {production_record.status})")
    if not production_record.lines:
        raise ProductionError("Cannot finalize a production entry with no product lines")
    production_record.status = STATUS_FINALIZED
    production_record.finalized_by = user.id
    production_record.finalized_at = _utcnow()
    production_record.updated_by = user.id


def reopen_production(production_record, user, reason):
    if production_record.status != STATUS_FINALIZED:
        raise ProductionError(f"Only a finalized production entry can be reopened (this one is {production_record.status})")
    if not reason:
        raise ProductionError("A reason is required to reopen a finalized production entry")
    production_record.status = STATUS_DRAFT
    production_record.finalized_by = None
    production_record.finalized_at = None
    production_record.remarks = (production_record.remarks + "\n\n" if production_record.remarks else "") + f"Reopened: {reason}"
    production_record.updated_by = user.id


def void_production(production_record, user, reason):
    if production_record.status == STATUS_VOID:
        raise ProductionError("Production entry is already void")
    if not reason:
        raise ProductionError("A reason is required to void a production entry")
    production_record.status = STATUS_VOID
    production_record.voided_by = user.id
    production_record.voided_at = _utcnow()
    production_record.void_reason = reason
    production_record.updated_by = user.id
