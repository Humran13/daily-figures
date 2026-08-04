from flask import Blueprint, Response, current_app, jsonify, request

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


@daily_figures_bp.route("/operator-summary", methods=["GET"])
@login_required
@feature_required("daily_figures")
def operator_summary():
    """
    Operator Daily Figures redesign — the ONE batched, authoritative
    response backing the Operator's compact read-only table (and reusable
    by any other read-only summary surface). Server-side role-authorized:
    reachable by any authenticated role (matching every other read-only
    Daily Figures endpoint), but returns exactly the same shared
    stock_service.daily_figure_view() data every other screen already
    uses — never a second, independently-reconstructed calculation, and
    never editable through this endpoint (it is GET-only; the only write
    path remains POST /api/daily-figures, still fully role/permission-
    gated there).

    Load-error correction — a period-WIDE, not per-row, decision between
    two modes:
      - "activity": at least one active product has genuine finalized
        Production, Returns, or Issued for this exact Date + Shift — the
        table shows ONLY those qualifying products, with their real
        authoritative values (opening/production/returns/issued/closing
        straight from daily_figure_view(), never recomputed).
      - "preview": no product anywhere has any such activity yet. EVERY
        active product is still listed (so the Operator always sees the
        full table scaffold, never an empty/error panel), but
        Production/Returns/Issued are always null placeholders (there is,
        by definition, no real movement to show), and Opening/Closing are
        only ever real numbers when they come from an "active clean
        ledger value" — on or after an ACTIVATED ledger cutover (see
        ledger_cutover_service.get_active_cutover() and
        daily_figure_view()'s own is_pre_cutover field) — never an
        invented zero, and never a pre-cutover/legacy-derived figure
        (which could be a messy or genuinely broken historical number)
        shown merely to fill the scaffold.

    The root cause of the previous generic "Unable to load" failure: a
    product that has never had a packaging rule configured (a perfectly
    normal, real intermediate state — see admin_products.create_product(),
    which creates a product active with no rule at all) makes
    daily_figure_view() return None for production/return_/issued/opening/
    closing, and the old code unconditionally subscripted those dicts —
    an unhandled TypeError, a Flask HTML 500 page, and a JSON-parse
    failure in the browser. Such a product can never have valid computed
    figures (there is no ratio to convert against) — it is now guarded
    explicitly, still listed by name in preview mode, but excluded from
    ever qualifying as "activity" and shown with placeholders throughout.
    Any OTHER unexpected failure is caught, logged server-side with the
    full traceback (see current_app.logger.exception below — never sent
    to the browser), and reported as a clean, generic JSON error instead
    of leaking implementation detail.
    """
    date, shift, err = _require_date_shift()
    if err:
        return err

    try:
        from webapp.services import ledger_cutover_service as cutover_svc
        from webapp.services import product_usage_service
        ranked_products = product_usage_service.ranked_active_products()

        all_rows = []
        activity_rows = []
        for product in ranked_products:
            view = svc.daily_figure_view(product, date, shift)
            has_rule = view["packaging_rule"] is not None
            all_rows.append((product, view, has_rule))
            if has_rule and (view["production"]["base_qty"] != 0 or view["return_"]["base_qty"] != 0 or view["issued"]["base_qty"] != 0):
                activity_rows.append(view)

        from webapp.models.dispatch import STATUS_FINALIZED as DISPATCH_FINALIZED, Dispatch
        from webapp.models.production_record import STATUS_FINALIZED as PRODUCTION_FINALIZED, ProductionRecord
        from webapp.models.return_record import STATUS_FINALIZED as RETURNS_FINALIZED, ReturnRecord

        production_count = ProductionRecord.query.filter_by(date=date, shift=shift, status=PRODUCTION_FINALIZED).count()
        # Dispatch/Returns are Day-only by the same established rule every
        # other surface in this app already follows — never invented here.
        dispatch_count = Dispatch.query.filter_by(date=date, shift=shift, status=DISPATCH_FINALIZED).count() if shift == "Day" else 0
        returns_count = ReturnRecord.query.filter_by(date=date, status=RETURNS_FINALIZED).count() if shift == "Day" else 0

        if activity_rows:
            mode = "activity"
            rows = activity_rows
        else:
            mode = "preview"
            active_cutover = cutover_svc.get_active_cutover(date, shift)
            rows = []
            for product, view, has_rule in all_rows:
                clean_ledger_value = has_rule and active_cutover is not None and not view["is_pre_cutover"]
                rows.append({
                    "product_id": product.id, "product_name": product.name,
                    "packaging_rule": view["packaging_rule"],
                    "opening": view["opening"] if clean_ledger_value else None,
                    "production": None, "return_": None, "issued": None,
                    "closing": view["closing"] if clean_ledger_value else None,
                })

        return jsonify({
            "date": date, "shift": shift,
            "mode": mode,
            "products": rows,
            "products_worked_on": len(activity_rows),
            "production_records": production_count,
            "return_records": returns_count,
            "dispatch_records": dispatch_count,
        })
    except Exception:
        current_app.logger.exception("operator_summary failed for date=%s shift=%s", date, shift)
        return jsonify({"error": "Could not load Daily Figures for this period. Please try again."}), 500


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
            # Final reset-safety correction — an ordinary Daily Figures
            # save (navigation, review, notes, status changes, No
            # Activity, routine paging) omits/sends false here, exactly
            # as every one of those flows already does today, since none
            # of them was ever updated to send anything else. Only a
            # dedicated Opening Stock correction interaction sends true —
            # see static/index.html's correctOpeningStock() flow. Role
            # authorization is re-validated inside upsert_daily_figure()
            # itself, never trusted from this flag alone.
            opening_stock_explicitly_edited=bool(d.get("opening_stock_explicitly_edited")),
            opening_correction_reason=d.get("opening_correction_reason"),
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
        ("issued_qty", "Issued"), ("closing_stock", "Closing Stock"), ("notes", "Notes"), ("ledger_period", "Ledger Period"),
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
            # Final stock architecture, ledger boundary rule — every
            # export row is explicitly labeled pre- or post-cutover,
            # never left to be inferred or silently mixed.
            "ledger_period": "Pre-cutover (reference only)" if view.get("is_pre_cutover") else "Active ledger",
        })

    from webapp.services import ledger_cutover_service as _cutover_svc
    active_cutover = _cutover_svc.get_active_cutover()
    if active_cutover is not None:
        filters_applied["ledger_boundary"] = (
            f"New verified stock ledger begins on {active_cutover.effective_date} - {active_cutover.effective_shift}. "
            "Historical legacy figures before this are preserved for reference and do not contribute to the active balance."
        )

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
