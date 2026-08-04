"""
Cross-cutting reports that don't map onto a single resource's own list
endpoint. Dispatch transactions, Daily Figures, and per-product/per-customer
history are all already exportable straight from their own filtered list
(GET /api/dispatches/export.<fmt>, GET /api/daily-figures/export.<fmt>) —
this blueprint is for shapes that genuinely don't exist anywhere else: a
date-range summary aggregated across the whole range, and totals grouped
by sales category or by recipient.
"""
from flask import Blueprint, Response, jsonify, request

from webapp.auth import current_user, roles_required, feature_required
from webapp.extensions import db
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import branding_service, stock_service as svc
from webapp.services.audit_service import record_audit
from webapp.services.export_service import MIME_TYPES, build_export

reports_bp = Blueprint("reports", __name__, url_prefix="/api/reports")


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


@reports_bp.route("/summary", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("reporting")
def summary():
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    if not date_from or not date_to:
        return _error("date_from and date_to are required")

    return jsonify(svc.date_range_summary(date_from, date_to))


@reports_bp.route("/summary/export.<fmt>", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("reporting")
def export_summary(fmt):
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    if not date_from or not date_to:
        return _error("date_from and date_to are required")

    data = svc.date_range_summary(date_from, date_to)
    columns = [
        ("product_name", "Product"),
        ("opening_cartons", "Opening Cartons"), ("opening_packs", "Opening Packs"), ("opening_pieces", "Opening Pieces"),
        ("return_cartons", "Return Cartons"), ("return_packs", "Return Packs"), ("return_pieces", "Return Pieces"),
        ("production_cartons", "Production Cartons"), ("production_packs", "Production Packs"), ("production_pieces", "Production Pieces"),
        ("issued_cartons", "Issued Cartons"), ("issued_packs", "Issued Packs"), ("issued_pieces", "Issued Pieces"),
        ("closing_cartons", "Closing Cartons"), ("closing_packs", "Closing Packs"), ("closing_pieces", "Closing Pieces"),
    ]

    def part(row, field, key):
        p = row.get(field)
        return p.get(key, "") if p else ""

    rows = [{
        "product_name": row["product_name"],
        "opening_cartons": part(row, "opening", "cartons"), "opening_packs": part(row, "opening", "packs"), "opening_pieces": part(row, "opening", "pieces"),
        "return_cartons": part(row, "return_", "cartons"), "return_packs": part(row, "return_", "packs"), "return_pieces": part(row, "return_", "pieces"),
        "production_cartons": part(row, "production", "cartons"), "production_packs": part(row, "production", "packs"), "production_pieces": part(row, "production", "pieces"),
        "issued_cartons": part(row, "issued", "cartons"), "issued_packs": part(row, "issued", "packs"), "issued_pieces": part(row, "issued", "pieces"),
        "closing_cartons": part(row, "closing", "cartons"), "closing_packs": part(row, "closing", "packs"), "closing_pieces": part(row, "closing", "pieces"),
    } for row in data]

    filters = {"date_from": date_from, "date_to": date_to}
    from webapp.services import ledger_cutover_service as _cutover_svc
    active_cutover = _cutover_svc.get_active_cutover()
    if active_cutover is not None:
        filters["ledger_boundary"] = (
            f"New verified stock ledger begins on {active_cutover.effective_date} - {active_cutover.effective_shift}. "
            "Historical legacy figures before this are preserved for reference and do not contribute to the active balance."
        )

    try:
        content = build_export(fmt, title="Date-Range Summary", filters=filters,
                                generated_by=current_user().username, columns=columns, rows=rows,
                                **branding_service.export_kwargs())
    except ValueError as e:
        return _error(e)

    record_audit(current_user(), "export", "summary_report",
                 after={"format": fmt, "date_from": date_from, "date_to": date_to, "row_count": len(rows)})
    db.session.commit()
    return Response(content, mimetype=MIME_TYPES[fmt],
                     headers={"Content-Disposition": f"attachment; filename=date_range_summary.{fmt}"})


def _recipient_totals_args():
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    group_by = request.args.get("group_by", "category")
    if not date_from or not date_to:
        raise ValueError("date_from and date_to are required")
    if group_by not in ("category", "recipient"):
        raise ValueError("group_by must be 'category' or 'recipient'")
    return date_from, date_to, group_by


@reports_bp.route("/recipient-totals", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("reporting")
def recipient_totals():
    """
    Total Issued and dispatch count grouped by sales category or by
    recipient — 'Total issued by sales category', 'Total issued by
    recipient', 'Dispatch count by category', 'Dispatch count by
    recipient' from the reporting requirements, all in one endpoint via
    ?group_by=category|recipient.
    """
    try:
        date_from, date_to, group_by = _recipient_totals_args()
    except ValueError as e:
        return _error(e)
    return jsonify(svc.recipient_totals(date_from, date_to, group_by))


@reports_bp.route("/recipient-totals/export.<fmt>", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("reporting")
def export_recipient_totals(fmt):
    try:
        date_from, date_to, group_by = _recipient_totals_args()
    except ValueError as e:
        return _error(e)

    data = svc.recipient_totals(date_from, date_to, group_by)
    columns = [
        ("group_name", "Sales Category" if group_by == "category" else "Recipient"),
        ("dispatch_count", "Dispatch Count"),
        ("total_issued_base_qty", "Total Issued (pieces)"),
    ]
    totals = {
        "group_name": "TOTAL",
        "dispatch_count": sum(r["dispatch_count"] for r in data),
        "total_issued_base_qty": sum(r["total_issued_base_qty"] for r in data),
    }

    try:
        content = build_export(
            fmt, title=f"Total Issued by {'Sales Category' if group_by == 'category' else 'Recipient'}",
            filters={"date_from": date_from, "date_to": date_to, "group_by": group_by},
            generated_by=current_user().username, columns=columns, rows=data, totals=totals,
            **branding_service.export_kwargs(),
        )
    except ValueError as e:
        return _error(e)

    record_audit(current_user(), "export", "recipient_totals_report",
                 after={"format": fmt, "date_from": date_from, "date_to": date_to, "group_by": group_by})
    db.session.commit()
    return Response(content, mimetype=MIME_TYPES[fmt],
                     headers={"Content-Disposition": f"attachment; filename=totals_by_{group_by}.{fmt}"})
