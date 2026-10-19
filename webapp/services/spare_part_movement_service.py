"""
Spare Parts movement ledger — the authoritative source of current stock.
Concurrency idiom matches the rest of the house (see
correction_request_service.consume_grant()): a conditional atomic
UPDATE + rowcount check, never SELECT ... FOR UPDATE. SparePart.
current_stock_cache exists ONLY as that concurrency gate; every display
path below independently sums the ledger in Decimal, so the two can
always be proven equal (see tests/test_spare_parts.py's reconciliation
test) rather than trusting the cache blindly.
"""
from decimal import Decimal

from sqlalchemy import update

from webapp.extensions import db
from webapp.models.spare_part import SparePart, is_discrete_unit
from webapp.models.spare_part_movement import (
    DECREASING_TYPES, INCREASING_TYPES, MOVEMENT_ADJUSTMENT_IN, MOVEMENT_ADJUSTMENT_OUT,
    MOVEMENT_STOCK_IN, MOVEMENT_STOCK_OUT, MOVEMENT_TYPES, REASON_OPENING_BALANCE,
    STATUS_ACTIVE, STATUS_VOID, SparePartMovement,
)
from webapp.services.audit_service import record_audit
from webapp.services.business_calendar import business_today


class SparePartMovementError(ValueError):
    pass


class InsufficientStockError(SparePartMovementError):
    pass


def current_stock(spare_part_id):
    """Authoritative current stock, derived from the ledger in Decimal —
    never trusts SparePart.current_stock_cache for display purposes."""
    total = Decimal("0")
    rows = (
        db.session.query(SparePartMovement.movement_type, SparePartMovement.quantity)
        .filter(SparePartMovement.spare_part_id == spare_part_id, SparePartMovement.status == STATUS_ACTIVE)
        .all()
    )
    for movement_type, quantity in rows:
        if movement_type in INCREASING_TYPES:
            total += quantity
        else:
            total -= quantity
    return total


def bulk_current_stock(spare_part_ids):
    """
    Authoritative current stock for MANY parts in one query — the same
    ledger derivation as current_stock() above, just grouped, so screens
    like Consumption & Procurement Planning never do one query per row
    (see spare_part_planning_service.py). Returns a dict keyed by every id
    in `spare_part_ids`, defaulting absent/no-movement parts to Decimal(0).
    """
    ids = list(spare_part_ids)
    stocks = {pid: Decimal("0") for pid in ids}
    if not ids:
        return stocks
    rows = (
        db.session.query(SparePartMovement.spare_part_id, SparePartMovement.movement_type, SparePartMovement.quantity)
        .filter(SparePartMovement.spare_part_id.in_(ids), SparePartMovement.status == STATUS_ACTIVE)
        .all()
    )
    for spare_part_id, movement_type, quantity in rows:
        if movement_type in INCREASING_TYPES:
            stocks[spare_part_id] = stocks.get(spare_part_id, Decimal("0")) + quantity
        else:
            stocks[spare_part_id] = stocks.get(spare_part_id, Decimal("0")) - quantity
    return stocks


def is_low_stock(spare_part, stock=None):
    stock = current_stock(spare_part.id) if stock is None else stock
    return stock_status_for(spare_part, stock) == "low"


def stock_status_for(spare_part, stock=None):
    """Return one mutually-exclusive inventory state.

    A positive-stock part without a configured threshold is healthy. This is
    the existing low-stock meaning (no threshold never raised an alert), made
    explicit rather than inventing an arbitrary fallback threshold.
    """
    stock = current_stock(spare_part.id) if stock is None else stock
    if stock <= 0:
        return "no_stock"
    if spare_part.minimum_stock is not None and stock <= spare_part.minimum_stock:
        return "low"
    return "healthy"


def validate_quantity_for_unit(quantity, unit, field="quantity", *, allow_zero=False):
    if quantity is None or not quantity.is_finite():
        raise SparePartMovementError(f"{field} must be a finite number")
    if quantity < 0 or (quantity == 0 and not allow_zero):
        qualifier = "zero or greater" if allow_zero else "greater than zero"
        raise SparePartMovementError(f"{field} must be {qualifier}")
    if is_discrete_unit(unit) and quantity != quantity.to_integral_value():
        raise SparePartMovementError(f"{field} must be a whole number for unit '{unit}'")
    return quantity


def _increase_cache(spare_part_id, quantity):
    db.session.execute(
        update(SparePart).where(SparePart.id == spare_part_id)
        .values(current_stock_cache=SparePart.current_stock_cache + quantity)
    )


def _decrease_cache(spare_part_id, quantity):
    result = db.session.execute(
        update(SparePart).where(SparePart.id == spare_part_id, SparePart.current_stock_cache >= quantity)
        .values(current_stock_cache=SparePart.current_stock_cache - quantity)
    )
    if result.rowcount == 0:
        raise InsufficientStockError("Insufficient stock for this operation.")


def _create_movement(*, spare_part, movement_type, quantity, user, business_date=None, **fields):
    if movement_type not in MOVEMENT_TYPES:
        raise SparePartMovementError(f"movement_type must be one of {MOVEMENT_TYPES}")
    validate_quantity_for_unit(quantity, spare_part.unit)
    if not spare_part.active:
        raise SparePartMovementError("Cannot record a movement against an inactive spare part")

    if movement_type in DECREASING_TYPES:
        _decrease_cache(spare_part.id, quantity)
    else:
        _increase_cache(spare_part.id, quantity)

    movement = SparePartMovement(
        business_date=business_date or business_today(),
        spare_part_id=spare_part.id,
        movement_type=movement_type,
        quantity=quantity,
        unit_snapshot=spare_part.unit,
        entered_by_user_id=user.id,
        status=STATUS_ACTIVE,
        **fields,
    )
    db.session.add(movement)
    db.session.flush()
    return movement


def record_stock_in(spare_part, quantity, user, *, business_date=None, supplier_id=None,
                     reference_doc=None, remarks=None):
    return _create_movement(
        spare_part=spare_part, movement_type=MOVEMENT_STOCK_IN, quantity=quantity, user=user,
        business_date=business_date, supplier_id=supplier_id, reference_doc=reference_doc, remarks=remarks,
    )


def record_stock_out(spare_part, quantity, user, *, business_date=None, requested_by=None,
                      approved_by=None, remarks=None, department_id=None, machine_id=None):
    if not requested_by:
        raise SparePartMovementError("requested_by is required for Stock Out")
    if machine_id is not None and department_id is not None:
        from webapp.models.machine import Machine
        machine = db.session.get(Machine, machine_id)
        if machine is None:
            raise SparePartMovementError("unknown machine_id")
        if machine.department_id is not None and machine.department_id != department_id:
            raise SparePartMovementError(
                "This machine belongs to a different department than the one selected."
            )
    return _create_movement(
        spare_part=spare_part, movement_type=MOVEMENT_STOCK_OUT, quantity=quantity, user=user,
        business_date=business_date, requested_by=requested_by, approved_by=approved_by, remarks=remarks,
        department_id=department_id, machine_id=machine_id,
    )


def record_adjustment(spare_part, quantity, direction, user, *, business_date=None, reason=None, remarks=None):
    """direction: 'in' or 'out'. Opening Balance is an 'in' adjustment with reason=REASON_OPENING_BALANCE."""
    movement_type = MOVEMENT_ADJUSTMENT_IN if direction == "in" else MOVEMENT_ADJUSTMENT_OUT
    if direction not in ("in", "out"):
        raise SparePartMovementError("direction must be 'in' or 'out'")
    return _create_movement(
        spare_part=spare_part, movement_type=movement_type, quantity=quantity, user=user,
        business_date=business_date, reason=reason, remarks=remarks,
    )


def record_stock_count(spare_part, counted_quantity, user, *, business_date=None, reason=None, remarks=None):
    """Record a physical count as a compensating ledger movement.

    The ledger remains authoritative. Both compared quantities are stored on
    the new movement so the correction can be reconstructed later.
    """
    validate_quantity_for_unit(counted_quantity, spare_part.unit, "counted_quantity", allow_zero=True)
    if not spare_part.active:
        raise SparePartMovementError("Cannot record a movement against an inactive spare part")
    reason = (reason or "").strip()
    if not reason:
        raise SparePartMovementError("A reason is required for a stock count correction")
    before = current_stock(spare_part.id)
    difference = counted_quantity - before
    movement_type = MOVEMENT_ADJUSTMENT_IN if difference >= 0 else MOVEMENT_ADJUSTMENT_OUT
    magnitude = abs(difference)

    if magnitude == 0:
        movement = SparePartMovement(
            business_date=business_date or business_today(), spare_part_id=spare_part.id,
            movement_type=movement_type, quantity=Decimal("0"), unit_snapshot=spare_part.unit,
            entered_by_user_id=user.id, status=STATUS_ACTIVE, reason=reason, remarks=remarks,
            system_quantity_before=before, counted_quantity=counted_quantity,
        )
        db.session.add(movement)
        db.session.flush()
        return movement

    return _create_movement(
        spare_part=spare_part, movement_type=movement_type, quantity=magnitude, user=user,
        business_date=business_date, reason=reason, remarks=remarks,
        system_quantity_before=before, counted_quantity=counted_quantity,
    )


def record_opening_balance(spare_part, quantity, user, *, business_date=None, remarks=None):
    return record_adjustment(spare_part, quantity, "in", user, business_date=business_date,
                              reason=REASON_OPENING_BALANCE, remarks=remarks)


def void_movement(movement, user, reason):
    if movement.status == STATUS_VOID:
        raise SparePartMovementError("Movement is already void")
    if not reason:
        raise SparePartMovementError("A reason is required to void a movement")

    # Inverse of the original effect — a voided STOCK_OUT/ADJUSTMENT_OUT
    # gives stock back, a voided STOCK_IN/ADJUSTMENT_IN takes it away
    # again (rejected below if that would go negative).
    if movement.movement_type in DECREASING_TYPES:
        _increase_cache(movement.spare_part_id, movement.quantity)
    else:
        try:
            _decrease_cache(movement.spare_part_id, movement.quantity)
        except InsufficientStockError:
            raise SparePartMovementError(
                "Cannot void: reversing this movement would make current stock negative."
            ) from None

    movement.status = STATUS_VOID
    movement.voided_by = user.id
    from webapp.services.business_calendar import utcnow
    movement.voided_at = utcnow()
    movement.void_reason = reason
    db.session.flush()
    return movement


# ---------- shared history filter/query (screen + exports use the same rows) ----------

def movement_history_args(args):
    exact_date = args.get("date")
    date_from = args.get("date_from") or exact_date
    date_to = args.get("date_to") or exact_date
    return {
        "date_from": date_from,
        "date_to": date_to,
        "month": args.get("month"),
        "spare_part_id": int(args["spare_part_id"]) if args.get("spare_part_id") else None,
        "machine_id": int(args["machine_id"]) if args.get("machine_id") else None,
        "movement_type": args.get("movement_type"),
        "requested_by": args.get("requested_by"),
    }


def movement_history(date_from=None, date_to=None, month=None, spare_part_id=None,
                      machine_id=None, movement_type=None, requested_by=None):
    query = SparePartMovement.query
    if month:
        query = query.filter(SparePartMovement.business_date.like(f"{month}%"))
    else:
        if date_from:
            query = query.filter(SparePartMovement.business_date >= date_from)
        if date_to:
            query = query.filter(SparePartMovement.business_date <= date_to)
    if spare_part_id:
        query = query.filter(SparePartMovement.spare_part_id == spare_part_id)
    if movement_type:
        query = query.filter(SparePartMovement.movement_type == movement_type)
    if requested_by:
        query = query.filter(SparePartMovement.requested_by.ilike(f"%{requested_by}%"))
    if machine_id:
        from webapp.models.spare_part import SparePartMachine
        query = query.join(SparePartMachine, SparePartMachine.spare_part_id == SparePartMovement.spare_part_id)
        query = query.filter(SparePartMachine.machine_id == machine_id)
    return query.order_by(SparePartMovement.business_date.desc(), SparePartMovement.id.desc()).all()


def movement_history_data(args):
    values = movement_history_args(args)
    return values, movement_history(**values)


# ---------- Requested By / Approved By suggestions ----------
# Reuses existing data sources only — active app usernames plus whatever
# free-text names have already been typed into the ledger. Never creates
# a user account; free typing always remains valid regardless of whether
# a name appears here.

def people_suggestions(query=None):
    from webapp.models.user import User
    names = set()
    for (username,) in db.session.query(User.username).filter_by(active=True).all():
        names.add(username)
    for (value,) in db.session.query(SparePartMovement.requested_by).filter(
        SparePartMovement.requested_by.isnot(None)
    ).distinct().all():
        if value:
            names.add(value)
    for (value,) in db.session.query(SparePartMovement.approved_by).filter(
        SparePartMovement.approved_by.isnot(None)
    ).distinct().all():
        if value:
            names.add(value)
    if query:
        q = query.strip().lower()
        names = {n for n in names if q in n.lower()}
    return sorted(names)[:50]


# ---------- dashboard ----------

def dashboard_summary():
    today = business_today()
    parts = SparePart.query.filter_by(active=True).all()
    stocks = {p.id: current_stock(p.id) for p in parts}

    stock_in_today = (
        SparePartMovement.query.filter_by(movement_type=MOVEMENT_STOCK_IN, status=STATUS_ACTIVE, business_date=today)
        .count()
    )
    stock_out_today = (
        SparePartMovement.query.filter_by(movement_type=MOVEMENT_STOCK_OUT, status=STATUS_ACTIVE, business_date=today)
        .count()
    )
    statuses = {p.id: stock_status_for(p, stocks[p.id]) for p in parts}
    low_stock_list = [
        {
            "id": p.id, "name": p.name, "specifications": p.specifications,
            "display_name": p.display_name, "unit": p.unit,
            "current_stock": str(stocks[p.id]), "minimum_stock": str(p.minimum_stock),
        }
        for p in parts if statuses[p.id] == "low"
    ]
    recent = (
        SparePartMovement.query.filter_by(status=STATUS_ACTIVE)
        .order_by(SparePartMovement.created_at.desc()).limit(10).all()
    )

    part_names = {p.id: p.display_name for p in SparePart.query.all()}
    recent_movements = []
    for movement in recent:
        row = movement.to_dict()
        row["spare_part_display_name"] = part_names.get(movement.spare_part_id, "")
        recent_movements.append(row)

    return {
        "total_active_spare_parts": len(parts),
        "healthy_stock_count": sum(1 for status in statuses.values() if status == "healthy"),
        "current_stock_items": sum(1 for v in stocks.values() if v > 0),
        "low_stock_count": len(low_stock_list),
        "no_stock_count": sum(1 for status in statuses.values() if status == "no_stock"),
        "low_stock": low_stock_list,
        "stock_in_today": stock_in_today,
        "stock_out_today": stock_out_today,
        "recent_movements": recent_movements,
    }
