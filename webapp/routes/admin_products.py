"""
Product and packaging-rule administration. Packaging rules are versioned:
PATCHing a product's rule never mutates the old row — it closes it
(effective_to = now) and inserts a new one, so historical dispatch lines
(which snapshot a rule id) keep the conversion that applied when they were
entered.
"""
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from webapp.auth import current_user, login_required, roles_required
from webapp.extensions import db
from webapp.models.dispatch import SHIFTS
from webapp.models.packaging_rule import PackagingRule
from webapp.models.product import Product
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services import daily_entry_status_service, product_usage_service
from webapp.services.audit_service import record_audit

admin_products_bp = Blueprint("admin_products", __name__, url_prefix="/api/admin/products")


def _utcnow():
    # Naive UTC, matching webapp/models — SQLite doesn't round-trip tzinfo.
    return datetime.now(timezone.utc).replace(tzinfo=None)


@admin_products_bp.route("", methods=["GET"])
@login_required
def list_products():
    """
    Default order is unchanged (display_order, name) — the admin Products &
    Packaging screen, and any other caller that doesn't ask for it, keeps
    exactly the same list it always had. Passing `?sort=usage` (Stage 8
    Part 2) switches to the global, shared quick-selection ranking instead
    — used by the operational Dispatch/Returns/Production/Daily Figures
    product selectors, never by admin configuration screens.

    Final pre-deployment correction: passing `?sort=usage` together with
    `date=&shift=` (as the Daily Figures wizard now does) additionally
    reorders into three priority groups for that exact date+shift —
    not-started, then in-progress, then completed/no-activity — each
    group still internally ordered by the same global usage ranking. This
    is a single extra batched query
    (daily_entry_status_service.bucket_for_date_shift(), one row set for
    every product at once) and a stable re-sort of the already-ranked
    list — never a query per product, and never a second/competing
    ranking system.
    """
    include_inactive = request.args.get("include_inactive") == "1"
    if request.args.get("sort") == "usage":
        products = product_usage_service.ranked_active_products(include_inactive=include_inactive)

        date = request.args.get("date")
        shift = request.args.get("shift")
        if date and shift in SHIFTS:
            buckets = daily_entry_status_service.bucket_for_date_shift(date, shift, [p.id for p in products])
            # Stable sort: within each bucket, the existing usage-rank
            # order (score desc, recency desc, total-uses desc,
            # display_order, name) from ranked_active_products() above is
            # preserved exactly — this only ever moves a product between
            # the three priority groups, never re-ranks within one.
            products.sort(key=lambda p: buckets.get(p.id, 0))

        results = []
        for p in products:
            d = p.to_dict()
            d["frequently_used"] = p.usage_score > 0
            results.append(d)
        return jsonify(results)

    query = Product.query.order_by(Product.display_order, Product.name)
    if not include_inactive:
        query = query.filter_by(active=True)
    return jsonify([p.to_dict() for p in query.all()])


@admin_products_bp.route("", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN)
def create_product():
    d = request.get_json(force=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if Product.query.filter_by(name=name).first():
        return jsonify({"error": "a product with this name already exists"}), 409

    max_order = db.session.query(db.func.max(Product.display_order)).scalar() or 0
    product = Product(
        name=name,
        display_order=d.get("display_order", max_order + 1),
        active=True,
        parent_product_id=d.get("parent_product_id"),
    )
    db.session.add(product)
    db.session.flush()
    record_audit(current_user(), "create", "product", entity_id=product.id, after=d)
    db.session.commit()
    return jsonify(product.to_dict()), 201


@admin_products_bp.route("/<int:product_id>", methods=["PATCH"])
@roles_required(ROLE_SUPER_ADMIN)
def update_product(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    before = product.to_dict()

    if "name" in d:
        new_name = d["name"].strip()
        if not new_name:
            return jsonify({"error": "name cannot be empty"}), 400
        clash = Product.query.filter(Product.name == new_name, Product.id != product.id).first()
        if clash:
            return jsonify({"error": "a product with this name already exists"}), 409
        product.name = new_name
    if "display_order" in d:
        product.display_order = int(d["display_order"])
    if "active" in d:
        product.active = bool(d["active"])
    if "parent_product_id" in d:
        product.parent_product_id = d["parent_product_id"]
    if "low_stock_threshold" in d:
        threshold = d["low_stock_threshold"]
        if threshold is not None and int(threshold) < 0:
            return jsonify({"error": "low_stock_threshold cannot be negative"}), 400
        product.low_stock_threshold = int(threshold) if threshold is not None else None

    record_audit(current_user(), "update", "product", entity_id=product.id, before=before, after=product.to_dict())
    db.session.commit()
    return jsonify(product.to_dict())


@admin_products_bp.route("/<int:product_id>/packaging-rules", methods=["GET"])
@login_required
def list_packaging_rules(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        return jsonify({"error": "not found"}), 404
    rules = sorted(product.packaging_rules, key=lambda r: r.effective_from, reverse=True)
    return jsonify([r.to_dict() for r in rules])


@admin_products_bp.route("/<int:product_id>/packaging-rules", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN)
def create_packaging_rule(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        return jsonify({"error": "not found"}), 404

    d = request.get_json(force=True) or {}
    cartons_to_packs = d.get("cartons_to_packs")
    packs_to_pieces = d.get("packs_to_pieces")
    carton_to_pieces = d.get("carton_to_pieces")

    has_pack_tier = cartons_to_packs is not None and packs_to_pieces is not None
    has_direct = carton_to_pieces is not None
    if has_pack_tier == has_direct:
        return jsonify({
            "error": "provide either (cartons_to_packs AND packs_to_pieces) for a pack tier, "
                     "or carton_to_pieces alone for a no-pack-tier product — not both, not neither"
        }), 400
    if has_pack_tier and (int(cartons_to_packs) < 1 or int(packs_to_pieces) < 1):
        return jsonify({"error": "cartons_to_packs and packs_to_pieces must be positive"}), 400
    if has_direct and int(carton_to_pieces) < 1:
        return jsonify({"error": "carton_to_pieces must be positive"}), 400

    now = _utcnow()
    current = product.current_packaging_rule(at=now)
    if current is not None:
        current.effective_to = now

    rule = PackagingRule(
        product_id=product.id,
        cartons_to_packs=int(cartons_to_packs) if has_pack_tier else None,
        packs_to_pieces=int(packs_to_pieces) if has_pack_tier else None,
        carton_to_pieces=int(carton_to_pieces) if has_direct else None,
        effective_from=now,
        effective_to=None,
        created_by=current_user().id,
    )
    db.session.add(rule)
    db.session.flush()
    record_audit(
        current_user(), "create", "packaging_rule", entity_id=rule.id,
        before=current.to_dict() if current else None, after=rule.to_dict(),
    )
    db.session.commit()
    return jsonify(rule.to_dict()), 201
