from flask import Blueprint, Response, jsonify, request

from webapp.auth import current_user, login_required, roles_required, feature_required
from webapp.extensions import db
from webapp.models.daily_figure import DailyFigure, StockAdjustment
from webapp.models.dispatch import SHIFTS
from webapp.models.product import Product
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN, ROLE_VIEWER
from webapp.services import branding_service
from webapp.services import daily_entry_status_service as entry_status_svc
from webapp.services import operator_permissions_service as permissions_svc
from webapp.services import stock_service as svc
from webapp.services.audit_service import record_audit
from webapp.services.export_service import MIME_TYPES, build_export
from webapp.services.quantity_format import qty_label
from webapp.services.stock_service import StockError

daily_figures_bp = Blueprint("daily_figures", __name__, url_prefix="/api/daily-figures")


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


def _require_date_shift():
    date = request.args.get("date")
    shift = request.args.get("shift")
    if not date or shift not in SHIFTS:
        return None, None, _error(f"date and shift (one of {SHIFTS}) are required")
    return date, shift, None


@daily_figures_bp.route("", methods=["GET"])
@login_required
@feature_required("daily_figures")
def list_views():
    date, shift, err = _require_date_shift()
    if err:
        return err
    products = Product.query.filter_by(active=True).order_by(Product.display_order, Product.name).all()
    return jsonify([svc.daily_figure_view(p, date, shift) for p in products])


@daily_figures_bp.route("/<int:product_id>", methods=["GET"])
@login_required
@feature_required("daily_figures")
def get_view(product_id):
    date, shift, err = _require_date_shift()
    if err:
        return err
    product = db.session.get(Product, product_id)
    if product is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(svc.daily_figure_view(product, date, shift))


def _qty_or_zero(part):
    if not part:
        return {"cartons": 0, "packs": 0, "pieces": 0}
    return {"cartons": part.get("cartons", 0), "packs": part.get("packs", 0), "pieces": part.get("pieces", 0)}


def _qty_changed(submitted, current):
    submitted = submitted or {}
    return (
        int(submitted.get("cartons", 0) or 0) != current["cartons"]
        or int(submitted.get("packs", 0) or 0) != current["packs"]
        or int(submitted.get("pieces", 0) or 0) != current["pieces"]
    )


@daily_figures_bp.route("", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("daily_figures")
def upsert():
    """
    Opening Stock is the only quantity Daily Figures still accepts
    directly (for a product's very first-ever period). Return and
    Production are no longer part of this payload as of Stage 5 — both are
    entered in the Returns Book / Production Book and read here live (see
    stock_service.daily_figure_view()); any 'return_'/'production' keys a
    caller still sends are ignored, never stored, exactly like Issued
    already was before this stage.
    """
    d = request.get_json(force=True) or {}
    user = current_user()

    for f in ("product_id", "date", "shift"):
        if f not in d:
            return _error(f"missing field: {f}")
    if d["shift"] not in SHIFTS:
        return _error(f"shift must be one of {SHIFTS}")

    product = db.session.get(Product, d["product_id"])
    if product is None:
        return _error("product does not exist")

    before = svc.daily_figure_view(product, d["date"], d["shift"])

    # Ownership/completion (Stage 7 section 1): an Operator who already sees
    # this entry as locked by someone else, or already completed, must not
    # even reach the permission/write logic below — Manager/Super Admin
    # correction authority (section 3) bypasses this entirely, same as it
    # already bypasses the Opening Stock permission flag just below.
    if user.role == ROLE_OPERATOR:
        conflict = entry_status_svc.check_operator_conflict(d["date"], d["shift"], product.id, user)
        if conflict:
            return _error(conflict, status=409)

    # Manager/Super Admin keep their existing unconditional write access to
    # Opening Stock. Operator is gated by the role-wide permission flag.
    # Issued/Returns/Production are never writable here at all (nothing in
    # this payload can touch any of them), so there is nothing further to
    # check for those.
    if user.role == ROLE_OPERATOR:
        permissions = permissions_svc.get_permissions()
        if before.get("opening_editable") and not permissions.can_edit_opening \
                and _qty_changed(d.get("opening"), _qty_or_zero(before.get("opening"))):
            return _error("you do not have permission to edit Opening Stock", status=403)

    try:
        figure = svc.upsert_daily_figure(
            product=product, date=d["date"], shift=d["shift"],
            opening=d.get("opening"), notes=d.get("notes"), user=user,
        )
    except StockError as e:
        db.session.rollback()
        return _error(e)

    # The actual concurrency-safe gate (a compare-and-swap UPDATE, not just
    # the pre-check above) — if another Operator's completion committed in
    # the narrow window between that pre-check and here, this raises and
    # the rollback below discards the DailyFigure write too, so no
    # duplicate/conflicting row and no double-counted total ever lands.
    #
    # Final pre-deployment correction — Manager/Super Administrator review
    # workflow: a review-mode save (the "Next Product" action while
    # reviewing, see webapp/routes/daily_review.py) explicitly opts out of
    # this per-product completion claim. Without this, EVERY elevated save
    # — including a no-op resave made purely by navigating past an
    # already-correct product — silently marked that product "completed",
    # which is exactly the false-completion problem this correction fixes.
    # Operators are entirely unaffected: `review_mode` is only ever
    # consulted in the non-Operator branch, and the operational workflow
    # never sends it.
    review_mode = bool(d.get("review_mode"))
    try:
        if user.role == ROLE_OPERATOR:
            entry_status_svc.mark_completed_with_data(d["date"], d["shift"], product.id, user)
        elif not review_mode:
            entry_status_svc.mark_completed_with_data_if_not_already(d["date"], d["shift"], product.id, user)
    except entry_status_svc.DailyEntryStatusConflict as e:
        db.session.rollback()
        return _error(e, status=409)

    after = svc.daily_figure_view(product, d["date"], d["shift"])
    record_audit(user, "upsert", "daily_figure", entity_id=f"{d['date']}|{d['shift']}|{product.name}",
                 before=before, after=after)
    db.session.commit()
    return jsonify(after)


@daily_figures_bp.route("/issued-detail", methods=["GET"])
@login_required
@feature_required("daily_figures")
def issued_detail():
    date, shift, err = _require_date_shift()
    if err:
        return err
    product_id = request.args.get("product_id")
    if not product_id:
        return _error("product_id is required")
    product = db.session.get(Product, int(product_id))
    if product is None:
        return jsonify({"error": "not found"}), 404

    sales_category_id = int(request.args["sales_category_id"]) if request.args.get("sales_category_id") else None
    customer_id = int(request.args["customer_id"]) if request.args.get("customer_id") else None
    return jsonify(svc.issued_detail(product, date, shift, sales_category_id=sales_category_id, customer_id=customer_id))


@daily_figures_bp.route("/adjustments", methods=["GET"])
@login_required
@feature_required("daily_figures")
def list_adjustments():
    query = StockAdjustment.query
    if request.args.get("date"):
        query = query.filter(StockAdjustment.date == request.args["date"])
    if request.args.get("shift"):
        query = query.filter(StockAdjustment.shift == request.args["shift"])
    if request.args.get("product_id"):
        query = query.filter(StockAdjustment.product_id == int(request.args["product_id"]))
    rows = query.order_by(StockAdjustment.created_at.desc()).limit(200).all()
    return jsonify([a.to_dict() for a in rows])


@daily_figures_bp.route("/adjustments", methods=["POST"])
@login_required
@feature_required("daily_figures")
def create_adjustment():
    user = current_user()
    if user.role == ROLE_VIEWER:
        return jsonify({"error": "forbidden"}), 403
    if user.role == ROLE_OPERATOR and not permissions_svc.get_permissions().can_create_adjustments:
        return jsonify({"error": "you do not have permission to create stock adjustments"}), 403

    d = request.get_json(force=True) or {}
    for f in ("product_id", "date", "shift", "delta_base_qty", "reason"):
        if not d.get(f) and d.get(f) != 0:
            return _error(f"missing field: {f}")

    product = db.session.get(Product, d["product_id"])
    if product is None:
        return _error("product does not exist")

    try:
        adjustment = svc.create_adjustment(
            product=product, date=d["date"], shift=d["shift"],
            delta_base_qty=int(d["delta_base_qty"]), reason=d["reason"], user=user,
        )
    except StockError as e:
        db.session.rollback()
        return _error(e)

    record_audit(user, "create", "stock_adjustment", entity_id=adjustment.id, after=adjustment.to_dict())
    db.session.commit()
    return jsonify(adjustment.to_dict()), 201


def _filtered_daily_figure_query(args):
    query = DailyFigure.query
    filters_applied = {}

    if args.get("date"):
        query = query.filter(DailyFigure.date == args["date"])
        filters_applied["date"] = args["date"]
    if args.get("date_from"):
        query = query.filter(DailyFigure.date >= args["date_from"])
        filters_applied["date_from"] = args["date_from"]
    if args.get("date_to"):
        query = query.filter(DailyFigure.date <= args["date_to"])
        filters_applied["date_to"] = args["date_to"]
    if args.get("shift"):
        query = query.filter(DailyFigure.shift == args["shift"])
        filters_applied["shift"] = args["shift"]
    if args.get("product_id"):
        query = query.filter(DailyFigure.product_id == int(args["product_id"]))
        product = db.session.get(Product, int(args["product_id"]))
        filters_applied["product"] = product.name if product else args["product_id"]

    return query, filters_applied


@daily_figures_bp.route("/history", methods=["GET"])
@login_required
@feature_required("daily_figures")
def history():
    query, _ = _filtered_daily_figure_query(request.args)
    limit = min(int(request.args.get("limit", 60)), 500)
    rows = query.order_by(DailyFigure.date.desc(), DailyFigure.shift, DailyFigure.id.desc()).limit(limit).all()
    return jsonify([svc.daily_figure_view(row.product, row.date, row.shift) for row in rows])


def _qty_str(part, rule):
    """
    One business-friendly quantity string per product ("3c 2p 5pc" / "3c
    5pc"), reusing the exact same packaging-rule-aware formatter the
    frontend already displays on screen — never a raw base-unit "Total
    Pieces" figure as the primary column, and never a second, duplicated
    conversion. A negative/unset closing shows its warning text instead.
    """
    if not part:
        return ""
    if "cartons" not in part:
        return part.get("warning", "")
    return qty_label(part["cartons"], part.get("packs", 0), part["pieces"], rule)


@daily_figures_bp.route("/export.<fmt>", methods=["GET"])
@login_required
@feature_required("daily_figures")
def export_daily_figures(fmt):
    query, filters_applied = _filtered_daily_figure_query(request.args)
    rows_db = query.order_by(DailyFigure.date, DailyFigure.shift, DailyFigure.product_id).limit(5000).all()

    columns = [
        ("date", "Date"), ("shift", "Shift"), ("product_name", "Product"),
        ("opening_stock", "Opening Stock"), ("return_qty", "Return"), ("production_qty", "Production"),
        ("issued_qty", "Issued"), ("closing_stock", "Closing Stock"), ("notes", "Notes"),
    ]
    rows = []
    for row in rows_db:
        view = svc.daily_figure_view(row.product, row.date, row.shift)
        rule = view["packaging_rule"]
        rows.append({
            "date": view["date"], "shift": view["shift"], "product_name": view["product_name"],
            "opening_stock": _qty_str(view["opening"], rule),
            "return_qty": _qty_str(view["return_"], rule),
            "production_qty": _qty_str(view["production"], rule),
            "issued_qty": _qty_str(view["issued"], rule),
            "closing_stock": _qty_str(view["closing"], rule),
            "notes": view["notes"] or "",
        })

    try:
        content = build_export(fmt, title="Daily Figures", filters=filters_applied,
                                generated_by=current_user().username, columns=columns, rows=rows,
                                **branding_service.export_kwargs())
    except ValueError as e:
        return _error(e)

    record_audit(current_user(), "export", "daily_figure", after={"format": fmt, "filters": filters_applied, "row_count": len(rows)})
    db.session.commit()
    return Response(content, mimetype=MIME_TYPES[fmt],
                     headers={"Content-Disposition": f"attachment; filename=daily_figures_export.{fmt}"})
