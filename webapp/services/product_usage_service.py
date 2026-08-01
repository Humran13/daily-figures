"""
Stage 8 Part 2 — global product "quick selection" ranking.

Usage events (webapp.models.product_usage_event.ProductUsageEvent) are
recorded only when a Dispatch/Returns/Production record is FINALIZED (one
event per distinct product touched by that record — never per line, never
per dropdown open, never per draft), and removed when that record is
VOIDED (a reversed record is no longer real completed activity). Re-
finalizing the same record after a reopen/correction upserts the existing
event's timestamp rather than creating a new one — see record_usage()'s
docstring for the exact idempotency guarantee.

Ranking score (Stage 8 section 12) — simple, deterministic, integer-only,
computed fresh from `now` on every read (no background job, no stored
score to go stale):

    active_score = 5 * (uses in the last 30 days)
                 + 2 * (uses 31-90 days ago)
                 + 1 * (uses 91-180 days ago)
    (uses older than 180 days contribute 0 to active_score)

Sort order: active_score DESC, most-recent-use DESC, all-time use count
DESC (tie-breaker for two products with the same recent activity, or none
at all), Product.display_order ASC, Product.name ASC.
"""
from datetime import datetime, timedelta, timezone

from webapp.extensions import db
from webapp.models.product import Product
from webapp.models.product_usage_event import ProductUsageEvent, SOURCES

RECENT_DAYS = 30
RECENT_POINTS = 5
MID_DAYS = 90
MID_POINTS = 2
OLD_DAYS = 180
OLD_POINTS = 1


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _validate(source, source_id, product_ids):
    if source not in SOURCES:
        raise ValueError(f"unknown usage source {source!r}")
    if not isinstance(source_id, int):
        raise ValueError("source_id must be an int")
    for pid in product_ids:
        if not isinstance(pid, int):
            raise ValueError("product_ids must be ints")


def record_usage(source, source_id, product_ids, used_at=None):
    """
    Called after a successful finalize() — `product_ids` is the exact,
    current set of distinct products in the record right now (duplicates
    within the record collapse to one event per product, matching "a valid
    record involving that product", not "a valid line").

    Idempotent and correction-safe: existing events for this
    (source, source_id) whose product is no longer in the current set are
    removed (a line was edited out before refinalizing); events for
    products still present just get `used_at` refreshed (a legitimate,
    recency-worthy re-finalization) rather than duplicated — the unique
    constraint on (source, source_id, product_id) is what guarantees a
    retried/duplicate request can never insert a second row for the same
    product either way.
    """
    product_ids = set(product_ids)
    _validate(source, source_id, product_ids)
    used_at = used_at or _utcnow()

    existing = {
        row.product_id: row
        for row in ProductUsageEvent.query.filter_by(source=source, source_id=source_id).all()
    }

    for product_id in existing.keys() - product_ids:
        db.session.delete(existing[product_id])

    for product_id in product_ids:
        row = existing.get(product_id)
        if row is None:
            db.session.add(ProductUsageEvent(source=source, source_id=source_id, product_id=product_id, used_at=used_at))
        else:
            row.used_at = used_at

    db.session.flush()


def remove_usage(source, source_id):
    """Called after void() — a voided record is no longer valid completed
    activity, so it must stop contributing to the ranking."""
    ProductUsageEvent.query.filter_by(source=source, source_id=source_id).delete()
    db.session.flush()


def ranked_active_products(include_inactive=False, now=None):
    """
    Every product (all-active by default, matching the existing
    GET /api/admin/products default), ordered by the Stage 8 ranking —
    frequently and recently used first, everything else still fully
    present and selectable. A single grouped query computes the score for
    every product with any usage at all; products with zero usage are
    simply absent from that query and sort last (score 0), never causing
    an N+1.
    """
    now = now or _utcnow()
    recent_cutoff = now - timedelta(days=RECENT_DAYS)
    mid_cutoff = now - timedelta(days=MID_DAYS)
    old_cutoff = now - timedelta(days=OLD_DAYS)

    score_case = db.case(
        (ProductUsageEvent.used_at >= recent_cutoff, RECENT_POINTS),
        (ProductUsageEvent.used_at >= mid_cutoff, MID_POINTS),
        (ProductUsageEvent.used_at >= old_cutoff, OLD_POINTS),
        else_=0,
    )
    usage_rows = (
        db.session.query(
            ProductUsageEvent.product_id,
            db.func.coalesce(db.func.sum(score_case), 0).label("score"),
            db.func.max(ProductUsageEvent.used_at).label("last_used_at"),
            db.func.count(ProductUsageEvent.id).label("total_uses"),
        )
        .group_by(ProductUsageEvent.product_id)
        .all()
    )
    usage_by_product = {row.product_id: row for row in usage_rows}

    query = Product.query
    if not include_inactive:
        query = query.filter_by(active=True)
    products = query.all()

    def usage_for(product):
        return usage_by_product.get(product.id)

    # Python's sort is stable, so applying it once per key from least to
    # most significant produces the correct composite ordering — this
    # avoids converting `last_used_at` to a numeric timestamp (datetime.min
    # is year 1, which .timestamp() cannot represent on every platform;
    # datetime objects compare directly with no such limit) just to negate
    # it for a single combined sort key.
    products.sort(key=lambda p: p.name)
    products.sort(key=lambda p: p.display_order)
    products.sort(key=lambda p: (usage_for(p).total_uses if usage_for(p) else 0), reverse=True)
    products.sort(key=lambda p: (usage_for(p).last_used_at if usage_for(p) else datetime.min), reverse=True)
    products.sort(key=lambda p: (usage_for(p).score if usage_for(p) else 0), reverse=True)

    # Transient, never-persisted attribute — a cheap way for the route to
    # know which products earned a "Frequently used" label without a
    # second query or exposing any user-level detail (just an aggregate
    # score, per spec section 15).
    for product in products:
        usage = usage_for(product)
        product.usage_score = usage.score if usage else 0

    return products
