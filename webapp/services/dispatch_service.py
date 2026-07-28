"""
Dispatch business rules: packaging conversion at entry time, duplicate
dispatch-number handling, and who is allowed to touch what. The backend is
the source of truth for all of this — nothing here is duplicated in
JavaScript as the "real" check.
"""
from datetime import datetime, timezone

from webapp.extensions import db
from webapp.models.customer import Customer
from webapp.models.dispatch import STATUS_DRAFT, STATUS_FINALIZED, STATUS_VOID, Dispatch, DispatchLine
from webapp.models.product import Product
from webapp.models.sales_category import SalesCategory
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import customer_service
from webapp.services.packaging import PackagingError, normalize, to_base_units


class DispatchError(ValueError):
    """Raised for user-facing validation problems (mapped to HTTP 400/409 by routes)."""


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def find_duplicate_number(dispatch_number, exclude_id=None):
    """Any non-void dispatch already using this number, if one exists."""
    query = Dispatch.query.filter(
        Dispatch.dispatch_number == dispatch_number,
        Dispatch.status != STATUS_VOID,
    )
    if exclude_id is not None:
        query = query.filter(Dispatch.id != exclude_id)
    return query.first()


def can_edit(dispatch, user):
    if user.role in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        return True
    # operator: only their own drafts
    return dispatch.status == STATUS_DRAFT and dispatch.created_by == user.id


def build_line(product_id, cartons, packs, pieces, line_notes=None, at=None):
    """
    Validate + convert one line's raw cartons/packs/pieces input into a
    DispatchLine-ready dict, snapshotting whichever packaging rule is
    active right now. Raises DispatchError for anything the user needs to
    fix before saving.
    """
    product = db.session.get(Product, product_id)
    if product is None or not product.active:
        raise DispatchError(f"Product {product_id} does not exist or is inactive")

    rule = product.current_packaging_rule(at=at)
    if rule is None:
        raise DispatchError(
            f"'{product.name}' has no packaging rule configured yet — "
            "set one in Admin > Products before dispatching it"
        )

    try:
        cartons, packs, pieces = normalize(cartons, packs, pieces, rule)
        base_units = to_base_units(cartons, packs, pieces, rule)
    except PackagingError as e:
        raise DispatchError(str(e)) from e

    if base_units == 0:
        raise DispatchError(f"'{product.name}' line has zero quantity")

    return {
        "product_id": product.id,
        "cartons": cartons,
        "packs": packs,
        "pieces": pieces,
        "base_unit_qty": base_units,
        "packaging_rule_id": rule.id,
        "line_notes": line_notes,
    }


def resolve_recipient(*, customer_id, new_customer_name, sales_category_id, user):
    """
    Two-step category -> recipient resolution shared by create_dispatch and
    update_recipient. Either customer_id (an existing recipient, which MUST
    belong to the given category if one is given — never silently re-homed
    into another category) or new_customer_name (creates a temporary
    recipient under the given category) must be supplied.

    If sales_category_id is omitted for an EXISTING customer_id, the
    category is derived from that customer's own current category rather
    than left blank — callers that need to guarantee a category is present
    (e.g. every new dispatch) must still check the returned category for
    None themselves, since a customer with no category at all yields None.
    """
    category = None
    if sales_category_id is not None:
        category = db.session.get(SalesCategory, sales_category_id)
        if category is None:
            raise DispatchError("sales category does not exist")

    if new_customer_name:
        if category is None:
            raise DispatchError("a sales category is required to create a new/temporary customer")
        customer = customer_service.create_temporary_customer(
            name=new_customer_name, sales_category_id=category.id, user=user,
        )
        return customer, category

    if not customer_id:
        raise DispatchError("customer_id or new_customer_name is required")
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        raise DispatchError("customer does not exist")

    if category is not None and customer.sales_category_id != category.id:
        raise DispatchError(
            f"'{customer.name}' belongs to a different sales category — "
            "a recipient cannot be silently saved under the wrong category. "
            "Reassign the recipient's category first if this is intentional."
        )
    if category is None:
        category = customer.sales_category
    return customer, category


def _require_category(customer, category):
    if category is None:
        raise DispatchError(
            f"A sales category is required — '{customer.name}' has no sales category assigned. "
            "Assign one in Admin > Customers, or select a category explicitly for this dispatch."
        )


def create_dispatch(*, dispatch_number, date, shift, customer_id=None, new_customer_name=None,
                     sales_category_id=None, invoice_number, notes,
                     lines, user, override_duplicate=False, override_reason=None):
    if not lines:
        raise DispatchError("A dispatch needs at least one product line")

    customer, category = resolve_recipient(
        customer_id=customer_id, new_customer_name=new_customer_name,
        sales_category_id=sales_category_id, user=user,
    )
    _require_category(customer, category)  # every NEW dispatch must have a category — no exceptions

    existing = find_duplicate_number(dispatch_number)
    duplicate_override = False
    if existing is not None:
        if not (override_duplicate and user.role == ROLE_SUPER_ADMIN):
            raise DispatchError(
                f"Dispatch number '{dispatch_number}' is already used by dispatch #{existing.id} "
                f"({existing.date}, {existing.customer.name if existing.customer else 'unknown customer'}). "
                "A super administrator can override this if it's a legitimate exception."
            )
        if not override_reason:
            raise DispatchError("A reason is required to override a duplicate dispatch number")
        duplicate_override = True

    now = _utcnow()
    built_lines = [build_line(**line, at=now) for line in lines]

    dispatch = Dispatch(
        dispatch_number=dispatch_number,
        date=date,
        shift=shift,
        customer_id=customer.id,
        sales_category_id=category.id if category else None,
        sales_category_name_snapshot=category.name if category else None,
        customer_name_snapshot=customer.name,
        invoice_number=invoice_number,
        notes=notes,
        status=STATUS_DRAFT,
        duplicate_override=duplicate_override,
        duplicate_override_reason=override_reason if duplicate_override else None,
        created_by=user.id,
        updated_by=user.id,
    )
    db.session.add(dispatch)
    db.session.flush()

    for line in built_lines:
        db.session.add(DispatchLine(dispatch_id=dispatch.id, **line))
    db.session.flush()

    return dispatch


def update_recipient(dispatch, *, customer_id, new_customer_name, sales_category_id, user):
    """
    The only supported way to change a draft dispatch's recipient and/or
    category — updates customer_id, sales_category_id,
    customer_name_snapshot, and sales_category_name_snapshot together as
    one in-memory mutation. The caller commits (or rolls back) the whole
    thing as a single transaction, so there is never a partial state where
    the FK ids and the historical-display snapshots disagree.
    """
    if dispatch.status != STATUS_DRAFT:
        raise DispatchError("only a draft dispatch's recipient can be edited directly — reopen it first")

    customer, category = resolve_recipient(
        customer_id=customer_id, new_customer_name=new_customer_name,
        sales_category_id=sales_category_id, user=user,
    )
    _require_category(customer, category)

    dispatch.customer_id = customer.id
    dispatch.sales_category_id = category.id
    dispatch.customer_name_snapshot = customer.name
    dispatch.sales_category_name_snapshot = category.name
    dispatch.updated_by = user.id
    return dispatch


def finalize_dispatch(dispatch, user):
    if dispatch.status != STATUS_DRAFT:
        raise DispatchError(f"Only a draft dispatch can be finalized (this one is {dispatch.status})")
    if not dispatch.lines:
        raise DispatchError("Cannot finalize a dispatch with no product lines")
    dispatch.status = STATUS_FINALIZED
    dispatch.finalized_by = user.id
    dispatch.finalized_at = _utcnow()
    dispatch.updated_by = user.id


def reopen_dispatch(dispatch, user, reason):
    """
    Move a finalized dispatch back to draft so its lines can be corrected —
    the deliberate gate in front of editing a finalized dispatch, so line
    changes never happen silently against a dispatch that already
    contributed to a stock total.
    """
    if dispatch.status != STATUS_FINALIZED:
        raise DispatchError(f"Only a finalized dispatch can be reopened (this one is {dispatch.status})")
    if not reason:
        raise DispatchError("A reason is required to reopen a finalized dispatch")
    dispatch.status = STATUS_DRAFT
    dispatch.finalized_by = None
    dispatch.finalized_at = None
    dispatch.notes = (dispatch.notes + "\n\n" if dispatch.notes else "") + f"Reopened: {reason}"
    dispatch.updated_by = user.id


def void_dispatch(dispatch, user, reason):
    if dispatch.status == STATUS_VOID:
        raise DispatchError("Dispatch is already void")
    if not reason:
        raise DispatchError("A reason is required to void a dispatch")
    dispatch.status = STATUS_VOID
    dispatch.voided_by = user.id
    dispatch.voided_at = _utcnow()
    dispatch.void_reason = reason
    dispatch.updated_by = user.id


def duplicate_dispatch(source, new_dispatch_number, user, sales_category_id=None):
    """
    Copy a dispatch's customer + lines into a new draft with a fresh
    number. This creates a NEW dispatch, so it must end up with a
    category too: normally carried forward from the source, but an
    override may be supplied for the (rare) case of duplicating a
    historical dispatch that predates category tracking.
    """
    if find_duplicate_number(new_dispatch_number) is not None:
        raise DispatchError(f"Dispatch number '{new_dispatch_number}' is already in use")

    now = _utcnow()
    source_customer = db.session.get(Customer, source.customer_id)

    if sales_category_id is not None:
        category = db.session.get(SalesCategory, sales_category_id)
        if category is None:
            raise DispatchError("sales category does not exist")
    else:
        category = db.session.get(SalesCategory, source.sales_category_id) if source.sales_category_id else None
        if category is None and source_customer is not None:
            category = source_customer.sales_category  # fall back to the customer's current category
    if category is None:
        raise DispatchError(
            "This dispatch has no sales category to carry forward — "
            "assign one to the recipient first, or pass sales_category_id explicitly."
        )
    if source_customer is not None and source_customer.sales_category_id not in (None, category.id):
        raise DispatchError(f"'{source_customer.name}' belongs to a different sales category than the one selected")

    new_dispatch = Dispatch(
        dispatch_number=new_dispatch_number,
        date=now.strftime("%Y-%m-%d"),
        shift=source.shift,
        customer_id=source.customer_id,
        sales_category_id=category.id,
        sales_category_name_snapshot=category.name,
        customer_name_snapshot=source_customer.name if source_customer else source.customer_name_snapshot,
        invoice_number=None,
        notes=f"Duplicated from dispatch #{source.id}",
        status=STATUS_DRAFT,
        created_by=user.id,
        updated_by=user.id,
    )
    db.session.add(new_dispatch)
    db.session.flush()

    for line in source.lines:
        # Re-run through build_line so the CURRENT packaging rule is
        # snapshotted, not the (possibly since-changed) rule the source used.
        built = build_line(line.product_id, line.cartons, line.packs, line.pieces, line.line_notes, at=now)
        db.session.add(DispatchLine(dispatch_id=new_dispatch.id, **built))
    db.session.flush()

    return new_dispatch
