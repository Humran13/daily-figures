"""
Returns Book business rules — packaging conversion at entry time and the
draft/finalized/void lifecycle. Mirrors webapp/services/dispatch_service.py
as closely as the two workflows allow: the same build_line()/normalize()/
to_base_units() reuse (never a second packaging implementation), the same
finalize/reopen/void shape. The differences are deliberate: no dispatch
number, no invoice, no sales category, and no shift — Returns is a
day-only workflow, so there is nothing here for a shift field to do.
"""
from datetime import datetime, timezone

from webapp.extensions import db
from webapp.models.customer import Customer
from webapp.models.product import Product
from webapp.models.return_record import STATUS_DRAFT, STATUS_FINALIZED, STATUS_VOID, ReturnLine, ReturnRecord
from webapp.services.packaging import PackagingError, normalize, to_base_units


class ReturnError(ValueError):
    """Raised for user-facing validation problems (mapped to HTTP 400/409 by routes)."""


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def can_edit(return_record, user):
    from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
    if user.role in (ROLE_SUPER_ADMIN, ROLE_MANAGER):
        return True
    return return_record.status == STATUS_DRAFT and return_record.created_by == user.id


def build_line(product_id, cartons, packs, pieces, line_notes=None, at=None):
    """Identical validation/conversion contract to dispatch_service.build_line()."""
    product = db.session.get(Product, product_id)
    if product is None or not product.active:
        raise ReturnError(f"Product {product_id} does not exist or is inactive")

    rule = product.current_packaging_rule(at=at)
    if rule is None:
        raise ReturnError(
            f"'{product.name}' has no packaging rule configured yet — "
            "set one in Admin > Products before recording a return for it"
        )

    try:
        cartons, packs, pieces = normalize(cartons, packs, pieces, rule)
        base_units = to_base_units(cartons, packs, pieces, rule)
    except PackagingError as e:
        raise ReturnError(str(e)) from e

    if base_units == 0:
        raise ReturnError(f"'{product.name}' line has zero quantity")

    return {
        "product_id": product.id,
        "cartons": cartons,
        "packs": packs,
        "pieces": pieces,
        "base_unit_qty": base_units,
        "packaging_rule_id": rule.id,
        "line_notes": line_notes,
    }


def _resolve_signed_by_name(signed_by_name, user):
    """
    "Name and sign" digital stand-in — server-authoritative, never a
    client-trusted value for an Operator. An Operator's signer identity is
    always their own authenticated username, no matter what the request
    body contains (a forged/edited frontend request could send any string
    otherwise) — the ONLY way an Operator's own return shows a different
    signer is by a Manager/Super Administrator later correcting it via the
    existing correction workflow, which this function has no say over.

    Manager/Super Administrator may sign as themselves (the default when
    nothing is supplied) or explicitly override to record another
    authorized person, exactly as the existing business workflow already
    allows for who receives/verifies a return.
    """
    from webapp.models.user import ROLE_OPERATOR
    if user.role == ROLE_OPERATOR:
        return user.username
    return (signed_by_name or "").strip() or user.username


def _resolve_returned_by(customer_id, returned_by_name):
    """
    Returned by is optional free text (a truck/route/field rep may not be a
    formal recipient) but reuses the Customer lookup whenever one is given,
    exactly like Dispatch's recipient field — never a second name table.
    """
    if customer_id:
        customer = db.session.get(Customer, customer_id)
        if customer is None:
            raise ReturnError("customer does not exist")
        return customer.id, customer.name
    return None, (returned_by_name or "").strip() or None


def create_return(*, date, returned_by_customer_id=None, returned_by_name=None, signed_by_name=None,
                   received_by=None, verified_by=None, remarks, lines, user):
    if not lines:
        raise ReturnError("A return needs at least one product line")

    customer_id, name_snapshot = _resolve_returned_by(returned_by_customer_id, returned_by_name)

    now = _utcnow()
    built_lines = [build_line(**line, at=now) for line in lines]

    record = ReturnRecord(
        date=date,
        returned_by_customer_id=customer_id,
        returned_by_name_snapshot=name_snapshot,
        signed_by_name=_resolve_signed_by_name(signed_by_name, user),
        # Defaults to whoever is entering the return — reuses the existing
        # session/user identity exactly like created_by/finalized_by
        # elsewhere, rather than needing a separate user-picker (the
        # user-list API is super_admin-only, so Operators/Managers
        # creating a return couldn't populate one anyway).
        received_by=received_by or user.id,
        verified_by=verified_by,
        remarks=remarks,
        status=STATUS_DRAFT,
        created_by=user.id,
        updated_by=user.id,
    )
    db.session.add(record)
    db.session.flush()

    for line in built_lines:
        db.session.add(ReturnLine(return_id=record.id, **line))
    db.session.flush()

    return record


def update_header(return_record, *, date=None, returned_by_customer_id=None, returned_by_name=None,
                   signed_by_name=None, received_by=None, verified_by=None, remarks=None, user):
    if return_record.status != STATUS_DRAFT:
        raise ReturnError("only a draft return's header can be edited directly — reopen it first")

    if date is not None:
        return_record.date = date
    if returned_by_customer_id is not None or returned_by_name is not None:
        customer_id, name_snapshot = _resolve_returned_by(returned_by_customer_id, returned_by_name)
        return_record.returned_by_customer_id = customer_id
        return_record.returned_by_name_snapshot = name_snapshot
    if signed_by_name is not None:
        return_record.signed_by_name = _resolve_signed_by_name(signed_by_name, user)
    if received_by is not None:
        return_record.received_by = received_by
    if verified_by is not None:
        return_record.verified_by = verified_by
    if remarks is not None:
        return_record.remarks = remarks
    return_record.updated_by = user.id
    return return_record


def finalize_return(return_record, user):
    if return_record.status != STATUS_DRAFT:
        raise ReturnError(f"Only a draft return can be finalized (this one is {return_record.status})")
    if not return_record.lines:
        raise ReturnError("Cannot finalize a return with no product lines")
    return_record.status = STATUS_FINALIZED
    return_record.finalized_by = user.id
    return_record.finalized_at = _utcnow()
    # Finalizing is the verification step in this workflow — whoever
    # finalizes a return is its verifier, unless one was already recorded
    # (e.g. a manager explicitly set verified_by to someone else beforehand).
    if return_record.verified_by is None:
        return_record.verified_by = user.id
    return_record.updated_by = user.id


def reopen_return(return_record, user, reason):
    if return_record.status != STATUS_FINALIZED:
        raise ReturnError(f"Only a finalized return can be reopened (this one is {return_record.status})")
    if not reason:
        raise ReturnError("A reason is required to reopen a finalized return")
    return_record.status = STATUS_DRAFT
    return_record.finalized_by = None
    return_record.finalized_at = None
    return_record.remarks = (return_record.remarks + "\n\n" if return_record.remarks else "") + f"Reopened: {reason}"
    return_record.updated_by = user.id


def void_return(return_record, user, reason):
    if return_record.status == STATUS_VOID:
        raise ReturnError("Return is already void")
    if not reason:
        raise ReturnError("A reason is required to void a return")
    return_record.status = STATUS_VOID
    return_record.voided_by = user.id
    return_record.voided_at = _utcnow()
    return_record.void_reason = reason
    return_record.updated_by = user.id


def delete_return(return_record, reason):
    """
    Permanent hard delete — Manager/Super Administrator only (enforced by
    the route's roles_required, not here). Mirrors
    dispatch_service.delete_dispatch() exactly: physically removes the
    ReturnRecord row; ReturnLine rows go with it via the ORM cascade
    already declared on ReturnRecord.lines. No status is ever set to any
    "deleted"/"void"/"cancelled" value — every live Returns-contribution
    calculation already reads ReturnRecord/ReturnLine straight from the
    database, so removal alone corrects it, no manual stock patching.
    """
    if not reason:
        raise ReturnError("A reason is required to permanently delete a return")
    db.session.delete(return_record)
    db.session.flush()
