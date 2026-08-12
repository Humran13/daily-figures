"""
Returns Book business rules — packaging conversion at entry time and the
draft/finalized/void lifecycle. Mirrors webapp/services/dispatch_service.py
as closely as the two workflows allow: the same build_line()/normalize()/
to_base_units() reuse (never a second packaging implementation), the same
finalize/reopen/void shape. The differences are deliberate: no dispatch
number, no invoice, no sales category, and no shift — Returns is a
day-only workflow, so there is nothing here for a shift field to do.
"""
from datetime import date as _date
from datetime import datetime, timezone

from webapp.extensions import db
from webapp.models.customer import Customer
from webapp.models.product import Product
from webapp.models.return_record import STATUS_DRAFT, STATUS_FINALIZED, STATUS_VOID, ReturnLine, ReturnRecord
from webapp.models.sales_category import SalesCategory
from webapp.services.packaging import PackagingError, normalize, to_base_units


class ReturnError(ValueError):
    """Raised for user-facing validation problems (mapped to HTTP 400/409 by routes)."""


# ---------------------------------------------------------------------
# Metro Sales business rule — Metro Sales (currently Dakar/Derrick)
# physically returns goods to store stock only on Monday, even though a
# Return may be recorded digitally on any day of the week. THE single
# authoritative rule lives here: is_return_stock_posting_eligible() for
# any code that already has one ReturnRecord in hand, and
# stock_posting_return_filter() (a SQLAlchemy expression built from the
# exact same two constants below) for the bulk aggregate queries in
# stock_service.py/stock_ledger_service.py that can't reasonably load
# every row into Python. No other module may re-derive "is this Metro
# Sales" or "is this Monday" independently — see METRO_SALES_CATEGORY_NAME
# and STOCK_POSTING_WEEKDAY.
# ---------------------------------------------------------------------

# The fixed, canonical Sales Category name seeded by migration
# 012e556ab7ad (see SALES_CATEGORIES there) — identification goes through
# this stable Customer.sales_category_id -> SalesCategory relationship,
# never through a recipient's own display name (Customer.name), which can
# be freely renamed without changing which sales channel it belongs to.
METRO_SALES_CATEGORY_NAME = "Metro Sales"

# Python's date.weekday(): Monday=0 .. Sunday=6. ReturnRecord.date is a
# plain "YYYY-MM-DD" calendar-date string (see return_record.py) with no
# time-of-day or timezone component to begin with, so weekday() on it is
# already the Africa/Kampala (EAT, UTC+3, no DST) business date — there is
# no separate conversion step.
STOCK_POSTING_WEEKDAY = 0


def is_metro_sales_return(return_record):
    """
    True when this return's recipient belongs to the canonical Metro
    Sales sales category (Customer.sales_category_id -> SalesCategory.name
    == "Metro Sales") — never by matching the recipient's display name.
    A return with no linked customer (free-text "returned by", e.g. a
    truck route with no formal recipient record) is never Metro Sales.
    """
    customer = return_record.returned_by_customer
    return (
        customer is not None
        and customer.sales_category is not None
        and customer.sales_category.name == METRO_SALES_CATEGORY_NAME
    )


def is_return_stock_posting_eligible(return_record):
    """
    THE single authoritative rule for whether a Return currently
    contributes to Daily Figures / stock — every stock-producing surface
    must go through this (or, for bulk SQL aggregation over many rows,
    stock_posting_return_filter() below, which encodes the exact same two
    rules and is the only other place this logic may be expressed).

    A normal (non-Metro-Sales) finalized return always posts, exactly as
    before this rule existed. A Metro Sales return posts only when BOTH
    finalized AND its own Return Date (never created_at/updated_at/today —
    the actual business date the recipient reported) falls on a Monday,
    the day Metro Sales's vehicle/team physically transfers goods back
    into store stock. Every other day, the return is a fully real,
    fully visible History record that simply contributes zero to stock
    until/unless it is later corrected onto a Monday.
    """
    if return_record.status != STATUS_FINALIZED:
        return False
    if not is_metro_sales_return(return_record):
        return True
    return _date.fromisoformat(return_record.date).weekday() == STOCK_POSTING_WEEKDAY


def stock_posting_return_filter():
    """
    The exact SQL-level restatement of is_return_stock_posting_eligible(),
    for AND-ing into any query whose FROM/JOIN already includes
    ReturnRecord (correlated on ReturnRecord.returned_by_customer_id) —
    used by stock_service.py's bulk Returns aggregate queries so the
    Monday/Metro-Sales rule is expressed in exactly one place even for
    SQL-side aggregation, never re-derived per query.
    """
    is_metro = (
        db.session.query(Customer.id)
        .join(SalesCategory, SalesCategory.id == Customer.sales_category_id)
        .filter(
            Customer.id == ReturnRecord.returned_by_customer_id,
            SalesCategory.name == METRO_SALES_CATEGORY_NAME,
        )
        .exists()
    )
    # SQLite strftime('%w', date) — '0'=Sunday .. '6'=Saturday, so Monday
    # (STOCK_POSTING_WEEKDAY=0 in Python's Mon=0..Sun=6 terms) is '1'.
    is_monday = db.func.strftime("%w", ReturnRecord.date) == "1"
    return db.and_(ReturnRecord.status == STATUS_FINALIZED, db.or_(~is_metro, is_monday))


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


def _find_active_duplicate_return(customer_id, date):
    """
    One active (draft or finalized — never void) Return per canonical
    recipient per business date. Resolves merges in both directions via
    customer_service's existing resolve_canonical()/
    resolve_customer_ids_for_filter() — reused verbatim (never a second
    merge-walking implementation) — so a Return recorded under a name
    later merged away, or under the customer it was later merged into,
    both still count as "the same recipient" for this check. Returns None
    (no duplicate) for a customer_id of None — a free-text-only "returned
    by" has no canonical relationship to check against, and inventing a
    fragile name-based uniqueness rule for that case risks breaking
    legitimate historical/free-text records; existing behavior for that
    edge case is preserved unchanged.
    """
    if customer_id is None:
        return None
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        return None
    from webapp.services.customer_service import resolve_canonical, resolve_customer_ids_for_filter
    canonical = resolve_canonical(customer)
    candidate_ids = resolve_customer_ids_for_filter(canonical.id)
    return (
        ReturnRecord.query.filter(
            ReturnRecord.date == date,
            ReturnRecord.returned_by_customer_id.in_(candidate_ids),
            ReturnRecord.status != STATUS_VOID,
        ).first()
    )


def create_return(*, date, returned_by_customer_id=None, returned_by_name=None, signed_by_name=None,
                   received_by=None, verified_by=None, remarks, lines, user):
    if not lines:
        raise ReturnError("A return needs at least one product line")

    customer_id, name_snapshot = _resolve_returned_by(returned_by_customer_id, returned_by_name)

    # One active Return per canonical recipient per business date — a
    # second entry for the same recipient/date is very likely an
    # accidental duplicate (this has already caused confusion for Metro
    # Sales recipients like Dakar), so it's rejected rather than silently
    # accepted as a second legitimate daily Return. Never merged, never
    # summed — the error message points the caller at the existing record
    # instead. A prior VOID record for the same recipient/date does NOT
    # block a legitimate replacement (void is "inactive" throughout this
    # app's existing status semantics — see e.g. dispatch_service.
    # find_duplicate_number()'s identical STATUS_VOID exclusion for
    # dispatch numbers). This is a server-side check — reached from every
    # caller of create_return(), so a forged API call is rejected exactly
    # like a normal one; the frontend has no separate/parallel check to
    # keep in sync.
    duplicate = _find_active_duplicate_return(customer_id, date)
    if duplicate is not None:
        raise ReturnError(
            f"A Return for {name_snapshot} already exists for {date}. "
            "Open the existing Return and edit/correct it instead."
        )

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
