from flask import Blueprint, jsonify, request

from webapp.auth import current_user, login_required, roles_required, feature_required
from webapp.extensions import db
from webapp.models.customer import CATEGORIES, Customer
from webapp.models.dispatch import Dispatch
from webapp.models.sales_category import SalesCategory
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN
from webapp.services import customer_service as svc
from webapp.services.audit_service import record_audit
from webapp.services.customer_service import CustomerServiceError, find_similar_customers

admin_customers_bp = Blueprint("admin_customers", __name__, url_prefix="/api/admin/customers")


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


@admin_customers_bp.route("", methods=["GET"])
@login_required
@feature_required("customer_management")
def list_customers():
    search = (request.args.get("q") or "").strip()
    include_inactive = request.args.get("include_inactive") == "1"
    query = Customer.query
    if not include_inactive:
        query = query.filter_by(active=True)
    if search:
        query = query.filter(Customer.name.ilike(f"%{search}%"))
    if request.args.get("sales_category_id"):
        query = query.filter(Customer.sales_category_id == int(request.args["sales_category_id"]))
    if request.args.get("is_temporary") is not None:
        query = query.filter(Customer.is_temporary == (request.args["is_temporary"] == "1"))
    # merged-away recipients are never offered for new dispatches
    query = query.filter(Customer.merged_into_id.is_(None))
    customers = query.order_by(Customer.name).limit(50).all()
    return jsonify([c.to_dict() for c in customers])


@admin_customers_bp.route("/check-duplicate", methods=["GET"])
@login_required
@feature_required("customer_management")
def check_duplicate():
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"matches": []})
    return jsonify({"matches": [c.to_dict() for c in find_similar_customers(name)]})


@admin_customers_bp.route("", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("customer_management")
def create_customer():
    d = request.get_json(force=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    category = d.get("category", "customer")
    if category not in CATEGORIES:
        return jsonify({"error": f"category must be one of {CATEGORIES}"}), 400

    sales_category_id = d.get("sales_category_id")
    if sales_category_id and db.session.get(SalesCategory, sales_category_id) is None:
        return _error("sales category does not exist")

    if not d.get("confirm_not_duplicate"):
        similar = find_similar_customers(name)
        if similar:
            return jsonify({
                "warning": "similar_customers_exist",
                "matches": [c.to_dict() for c in similar],
                "hint": "resend with confirm_not_duplicate: true to create anyway",
            }), 409

    customer = Customer(
        name=name,
        category=category,
        active=True,
        sales_category_id=sales_category_id,
        is_temporary=bool(d.get("is_temporary", False)),
        contact_info=d.get("contact_info"),
        notes=d.get("notes"),
        created_by=current_user().id,
    )
    db.session.add(customer)
    db.session.flush()
    record_audit(current_user(), "create", "customer", entity_id=customer.id, after=customer.to_dict())
    db.session.commit()
    return jsonify(customer.to_dict()), 201


@admin_customers_bp.route("/<int:customer_id>", methods=["PATCH"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("customer_management")
def update_customer(customer_id):
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}

    # Customer rename correction — renaming is Super Administrator only.
    # Every OTHER field this endpoint already lets Manager edit (category,
    # active, contact_info, notes, sales_category_id reassignment) is
    # untouched by this — only an attempt to touch "name" itself is
    # blocked here, enforced server-side regardless of what the frontend
    # shows (admin.html is already Super-Admin-only at the page level, but
    # that is never the real gate — this route is).
    if "name" in d and current_user().role != ROLE_SUPER_ADMIN:
        return jsonify({"error": "only a Super Administrator may rename a customer"}), 403

    before = customer.to_dict()

    if "name" in d:
        new_name = d["name"].strip()
        if not new_name:
            return jsonify({"error": "name cannot be empty"}), 400
        if new_name != customer.name:
            # Reuses the exact same duplicate-detection the create flow
            # already uses — a rename must not accidentally collide with
            # an existing recipient any more than creating one would.
            if not d.get("confirm_not_duplicate"):
                similar = find_similar_customers(new_name, exclude_id=customer.id)
                if similar:
                    return jsonify({
                        "warning": "similar_customers_exist",
                        "matches": [c.to_dict() for c in similar],
                        "hint": "resend with confirm_not_duplicate: true to rename anyway",
                    }), 409
            previous_name = customer.name
            svc.rename_customer(customer, new_name, current_user())
            record_audit(
                current_user(), "rename", "customer", entity_id=customer.id,
                before={"customer_id": customer.id, "previous_name": previous_name},
                after={"customer_id": customer.id, "new_name": new_name},
            )
    if "category" in d:
        if d["category"] not in CATEGORIES:
            return jsonify({"error": f"category must be one of {CATEGORIES}"}), 400
        customer.category = d["category"]
    if "active" in d:
        customer.active = bool(d["active"])
    if "contact_info" in d:
        customer.contact_info = d["contact_info"]
    if "notes" in d:
        customer.notes = d["notes"]
    if "sales_category_id" in d:
        try:
            svc.reassign_category(customer, d["sales_category_id"], current_user())
        except CustomerServiceError as e:
            db.session.rollback()
            return _error(e)

    record_audit(current_user(), "update", "customer", entity_id=customer.id, before=before, after=customer.to_dict())
    db.session.commit()
    return jsonify(customer.to_dict())


@admin_customers_bp.route("/<int:customer_id>/dispatches", methods=["GET"])
@login_required
@feature_required("customer_management")
def customer_dispatches(customer_id):
    """All dispatches recorded under this exact recipient record — used by
    the review screen so an admin can see the history before merging."""
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        return jsonify({"error": "not found"}), 404
    dispatches = (
        Dispatch.query.filter_by(customer_id=customer_id)
        .order_by(Dispatch.date.desc(), Dispatch.id.desc())
        .limit(200)
        .all()
    )
    return jsonify([d.to_dict(include_lines=False) for d in dispatches])


@admin_customers_bp.route("/temporary-review", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("customer_management")
def temporary_review_queue():
    customers = (
        Customer.query.filter_by(is_temporary=True, active=True)
        .order_by(Customer.created_at.desc())
        .all()
    )
    results = []
    for c in customers:
        dispatch_count = Dispatch.query.filter_by(customer_id=c.id).count()
        results.append({
            **c.to_dict(),
            "dispatch_count": dispatch_count,
            "similar_customers": [
                m.to_dict() for m in find_similar_customers(c.name, exclude_id=c.id)
            ],
        })
    return jsonify(results)


@admin_customers_bp.route("/<int:customer_id>/approve-temporary", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("customer_management")
def approve_temporary(customer_id):
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        return jsonify({"error": "not found"}), 404
    if not customer.is_temporary:
        return _error("this recipient is not marked temporary")

    before = customer.to_dict()
    customer.is_temporary = False
    record_audit(current_user(), "approve_temporary", "customer", entity_id=customer.id,
                 before=before, after=customer.to_dict())
    db.session.commit()
    return jsonify(customer.to_dict())


@admin_customers_bp.route("/<int:customer_id>/reject-temporary", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("customer_management")
def reject_temporary(customer_id):
    """
    Rejects a temporary recipient WITHOUT deleting it — its dispatches must
    keep working. This just deactivates it so it stops appearing in
    autocomplete; an admin can still find and fix it later.
    """
    customer = db.session.get(Customer, customer_id)
    if customer is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    reason = d.get("reason")
    if not reason:
        return _error("a reason is required to reject a temporary recipient")

    before = customer.to_dict()
    customer.active = False
    record_audit(current_user(), "reject_temporary", "customer", entity_id=customer.id,
                 before=before, after={**customer.to_dict(), "reason": reason})
    db.session.commit()
    return jsonify(customer.to_dict())


@admin_customers_bp.route("/<int:customer_id>/merge", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("customer_management")
def merge_customer(customer_id):
    source = db.session.get(Customer, customer_id)
    if source is None:
        return jsonify({"error": "not found"}), 404
    d = request.get_json(force=True) or {}
    target_id = d.get("target_customer_id")
    if not target_id:
        return _error("target_customer_id is required")
    target = db.session.get(Customer, target_id)
    if target is None:
        return _error("target customer does not exist")

    before = source.to_dict()
    try:
        result = svc.merge_customers(source, target, current_user(), reason=d.get("reason"))
    except CustomerServiceError as e:
        db.session.rollback()
        return _error(e)

    record_audit(current_user(), "merge", "customer", entity_id=source.id,
                 before=before, after={**source.to_dict(), "reason": d.get("reason"), **result})
    db.session.commit()
    return jsonify({"source": source.to_dict(), **result})
