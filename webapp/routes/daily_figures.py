from flask import Blueprint, Response, jsonify, request

from webapp.auth import current_user, login_required, roles_required, feature_required
from webapp.extensions import db
from webapp.models.daily_figure import DailyFigure, StockAdjustment
from webapp.models.dispatch import SHIFTS
from webapp.models.product import Product
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN, ROLE_VIEWER
from webapp.services import branding_service
from webapp.services import operator_permissions_service as permissions_svc
from webapp.services import stock_service as svc
from webapp.services.audit_service import record_audit
from webapp.services.export_service import MIME_TYPES, build_export
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
    d = request.get_json(force=True) or {}
    user = current_user()

    for f in ("product_id", "date", "shift", "return_", "production"):
        if f not in d:
            return _error(f"missing field: {f}")
    if d["shift"] not in SHIFTS:
        return _error(f"shift must be one of {SHIFTS}")

    product = db.session.get(Product, d["product_id"])
    if product is None:
        return _error("product does not exist")

    before = svc.daily_figure_view(product, d["date"], d["shift"])

    # Manager/Super Admin keep their existing unconditional write access.
    # Operator is gated per field by the role-wide permission flags — Issued
    # is never in this payload at all (it has no writable column anywhere),
    # so there is nothing here that could ever touch it.
    if user.role == ROLE_OPERATOR:
        permissions = permissions_svc.get_permissions()
        if before.get("opening_editable") and not permissions.can_edit_opening \
                and _qty_changed(d.get("opening"), _qty_or_zero(before.get("opening"))):
            return _error("you do not have permission to edit Opening stock", status=403)
        if not permissions.can_edit_returns and _qty_changed(d.get("return_"), _qty_or_zero(before.get("return_"))):
            return _error("you do not have permission to edit Returns", status=403)
        if not permissions.can_edit_production and _qty_changed(d.get("production"), _qty_or_zero(before.get("production"))):
            return _error("you do not have permission to edit Production", status=403)

    try:
        figure = svc.upsert_daily_figure(
            product=product, date=d["date"], shift=d["shift"],
            opening=d.get("opening"), return_=d["return_"], production=d["production"],
            notes=d.get("notes"), user=user,
        )
    except StockError as e:
        db.session.rollback()
        return _error(e)

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


def _qty_part(part, key):
    if not part or "cartons" not in part:
        return ""
    return part[key]


@daily_figures_bp.route("/export.<fmt>", methods=["GET"])
@login_required
@feature_required("daily_figures")
def export_daily_figures(fmt):
    query, filters_applied = _filtered_daily_figure_query(request.args)
    rows_db = query.order_by(DailyFigure.date, DailyFigure.shift, DailyFigure.product_id).limit(5000).all()

    columns = [
        ("date", "Date"), ("shift", "Shift"), ("product_name", "Product"),
        ("opening_cartons", "Opening Cartons"), ("opening_packs", "Opening Packs"), ("opening_pieces", "Opening Pieces"),
        ("return_cartons", "Return Cartons"), ("return_packs", "Return Packs"), ("return_pieces", "Return Pieces"),
        ("production_cartons", "Production Cartons"), ("production_packs", "Production Packs"), ("production_pieces", "Production Pieces"),
        ("issued_cartons", "Issued Cartons"), ("issued_packs", "Issued Packs"), ("issued_pieces", "Issued Pieces"),
        ("closing_cartons", "Closing Cartons"), ("closing_packs", "Closing Packs"), ("closing_pieces", "Closing Pieces"),
        ("notes", "Notes"),
    ]
    rows = []
    for row in rows_db:
        view = svc.daily_figure_view(row.product, row.date, row.shift)
        rows.append({
            "date": view["date"], "shift": view["shift"], "product_name": view["product_name"],
            "opening_cartons": _qty_part(view["opening"], "cartons"), "opening_packs": _qty_part(view["opening"], "packs"), "opening_pieces": _qty_part(view["opening"], "pieces"),
            "return_cartons": _qty_part(view["return_"], "cartons"), "return_packs": _qty_part(view["return_"], "packs"), "return_pieces": _qty_part(view["return_"], "pieces"),
            "production_cartons": _qty_part(view["production"], "cartons"), "production_packs": _qty_part(view["production"], "packs"), "production_pieces": _qty_part(view["production"], "pieces"),
            "issued_cartons": _qty_part(view["issued"], "cartons"), "issued_packs": _qty_part(view["issued"], "packs"), "issued_pieces": _qty_part(view["issued"], "pieces"),
            "closing_cartons": _qty_part(view["closing"], "cartons"), "closing_packs": _qty_part(view["closing"], "packs"), "closing_pieces": _qty_part(view["closing"], "pieces"),
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
