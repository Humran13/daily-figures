"""
Recipient (Customer) lifecycle beyond plain CRUD: similarity detection,
temporary-recipient creation, category reassignment with a best-effort
historical backfill, and merging that never deletes or orphans a
dispatch's history.
"""
import difflib

from webapp.extensions import db
from webapp.models.customer import Customer
from webapp.models.dispatch import Dispatch
from webapp.models.sales_category import SalesCategory

DUPLICATE_SIMILARITY_THRESHOLD = 0.6


class CustomerServiceError(ValueError):
    pass


def find_similar_customers(name, exclude_id=None):
    """Same threshold/approach as the existing quick-add duplicate warning (admin_customers.py)."""
    candidates = Customer.query.filter_by(active=True)
    if exclude_id is not None:
        candidates = candidates.filter(Customer.id != exclude_id)
    target = name.strip().lower()
    matches = []
    for c in candidates.all():
        ratio = difflib.SequenceMatcher(None, target, c.name.strip().lower()).ratio()
        if ratio >= DUPLICATE_SIMILARITY_THRESHOLD:
            matches.append(c)
    return matches


def resolve_canonical(customer):
    """Follow a merge chain to its root (handles a customer merged into a customer that was itself later merged)."""
    seen = set()
    current = customer
    while current.merged_into_id is not None and current.merged_into_id not in seen:
        seen.add(current.id)
        current = db.session.get(Customer, current.merged_into_id)
        if current is None:
            break
    return current


def resolve_customer_ids_for_filter(customer_id):
    """
    Every customer id that should count as 'this recipient' for search/
    filter/report purposes: the given id plus every id merged (directly or
    transitively) into it. Used so filtering by the canonical customer
    still surfaces dispatches originally recorded under a name later
    merged away.
    """
    ids = {customer_id}
    frontier = [customer_id]
    while frontier:
        current = frontier.pop()
        children = Customer.query.filter_by(merged_into_id=current).all()
        for child in children:
            if child.id not in ids:
                ids.add(child.id)
                frontier.append(child.id)
    return list(ids)


def backfill_dispatches_for_customer(customer):
    """
    Best-effort historical backfill: any of this customer's dispatches that
    predate category tracking (sales_category_id IS NULL) get the
    customer's CURRENT category stamped on — understanding this isn't
    strictly the category "as it applied at the time" since none existed
    then. Never touches a dispatch that already has a category snapshot,
    so this is safe to call every time a customer's category changes.
    """
    if customer.sales_category_id is None:
        return 0
    category = db.session.get(SalesCategory, customer.sales_category_id)
    if category is None:
        return 0

    dispatches = Dispatch.query.filter_by(customer_id=customer.id, sales_category_id=None).all()
    for d in dispatches:
        d.sales_category_id = category.id
        d.sales_category_name_snapshot = category.name
        if not d.customer_name_snapshot:
            d.customer_name_snapshot = customer.name
    db.session.flush()
    return len(dispatches)


def create_temporary_customer(*, name, sales_category_id, user, contact_info=None, notes=None):
    name = (name or "").strip()
    if not name:
        raise CustomerServiceError("name is required")
    customer = Customer(
        name=name, active=True, is_temporary=True, sales_category_id=sales_category_id,
        contact_info=contact_info, notes=notes, created_by=user.id,
    )
    db.session.add(customer)
    db.session.flush()
    return customer


def reassign_category(customer, new_sales_category_id, user):
    category = db.session.get(SalesCategory, new_sales_category_id) if new_sales_category_id else None
    if new_sales_category_id and category is None:
        raise CustomerServiceError("sales category does not exist")
    customer.sales_category_id = new_sales_category_id
    db.session.flush()
    backfilled = backfill_dispatches_for_customer(customer) if new_sales_category_id else 0
    return backfilled


def merge_customers(source, target, user, reason=None):
    if source.id == target.id:
        raise CustomerServiceError("cannot merge a recipient into itself")

    target = resolve_canonical(target)
    if target.id == source.id:
        raise CustomerServiceError("cannot merge a recipient into itself (target resolves back to source)")

    # if anything was already merged into `source`, repoint it directly at
    # the new target so the chain never has to be walked more than once
    dependents = Customer.query.filter_by(merged_into_id=source.id).all()
    for dep in dependents:
        dep.merged_into_id = target.id

    dispatch_count = Dispatch.query.filter_by(customer_id=source.id).count()

    source.merged_into_id = target.id
    source.active = False
    db.session.flush()
    return {"target_id": target.id, "dispatch_count_preserved": dispatch_count, "repointed_dependents": len(dependents)}
