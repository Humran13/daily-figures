"""
Consumption & Procurement Planning — read-only analytics on top of the
Spare Parts master/ledger data. Every Spare-Parts role may view the
operational columns (consumption, trend, coverage, priority, ABC class),
exactly like the existing Dashboard/History screens; money (unit prices,
China/Local projected cost, savings, cost-based summary cards) stays
Super-Administrator-only, mirroring webapp/routes/spare_parts.py's
_may_see_pricing() gate exactly — Manager does NOT get pricing here either,
consistent with how it is already withheld everywhere else in Spare Parts.

Marking a part Critical reuses the EXISTING PATCH /api/spare-parts/<id>
endpoint (webapp/services/spare_part_service.py's update_spare_part()
already accepts critical/critical_note) rather than a parallel endpoint.
Unlike most other spare-part master-data edits, changing Critical status
is Super-Administrator-only (see that route's _critical_forbidden_response())
— it drives procurement priority directly, so it is restricted the same
way pricing is, even though Manager may still edit this same spare part's
other fields. Already audited by that route's existing before/after diff.
"""
from flask import Blueprint, Response, jsonify, request

from webapp.auth import current_user, login_required
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services import branding_service
from webapp.services import spare_part_planning_service as planning_svc
from webapp.services.audit_service import record_audit
from webapp.services.export_service import MIME_TYPES, build_export
from webapp.extensions import db

spare_part_planning_bp = Blueprint(
    "spare_part_planning", __name__, url_prefix="/api/spare-parts/planning",
)


def _may_see_pricing():
    user = current_user()
    return user is not None and user.role == ROLE_SUPER_ADMIN


def _parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _report_kwargs():
    args = request.args
    horizon_months = _parse_int(args.get("horizon_months"), planning_svc.DEFAULT_HORIZON_MONTHS)
    analysis_period_days = _parse_int(args.get("analysis_period_days"), planning_svc.DEFAULT_ANALYSIS_PERIOD_DAYS)
    return {
        "horizon_months": horizon_months,
        "analysis_period_days": analysis_period_days,
        "date_from": args.get("date_from") or None,
        "date_to": args.get("date_to") or None,
        "source": args.get("source") or planning_svc.SOURCE_BEST_PRICE,
        "search": args.get("q") or None,
        "category_id": _parse_int(args.get("category_id"), None),
        "machine_id": _parse_int(args.get("machine_id"), None),
        "department_id": _parse_int(args.get("department_id"), None),
        "supplier_id": _parse_int(args.get("supplier_id"), None),
        "preferred_source": args.get("preferred_source") or None,
        "abc_class": (args.get("abc_class") or None),
        "critical_only": args.get("critical_only") == "1",
        "stock_status": args.get("stock_status") or None,
        "view": args.get("view") or None,
        "include_inactive": args.get("include_inactive") == "1",
        "include_pricing": _may_see_pricing(),
    }


@spare_part_planning_bp.route("", methods=["GET"])
@login_required
def planning_report():
    try:
        report = planning_svc.build_planning_report(**_report_kwargs())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(report)


_EXPORT_COLUMNS = [
    ("display_name", "Spare Part"), ("category_name", "Category"),
    ("unit", "Unit"), ("current_stock", "Current Stock"), ("minimum_stock", "Minimum Stock"),
    ("average_monthly_consumption", "Avg Monthly Consumption"),
    ("safety_stock", "Safety Stock"), ("incoming_stock", "Incoming Stock"),
    ("recommended_purchase_selected", "Recommended Purchase (Selected Horizon)"),
    ("selected_horizon_months", "Horizon (Months)"),
    ("preferred_cost_source", "Preferred Source"), ("selected_source", "Procurement Source"),
    ("abc_class", "ABC Class"), ("critical", "Critical"), ("procurement_priority", "Priority"),
    ("stock_status", "Stock Status"),
]
_EXPORT_PRICING_COLUMNS = [
    ("china_buying_price", "China Unit Price"), ("local_buying_price", "Local Unit Price"),
    ("selected_source_projected_cost", "Projected Cost (Selected Source)"),
    ("estimated_savings", "Estimated Savings"),
]


@spare_part_planning_bp.route("/export.<fmt>", methods=["GET"])
@login_required
def export_planning_report(fmt):
    if fmt not in MIME_TYPES:
        return jsonify({"error": "unsupported format"}), 400
    kwargs = _report_kwargs()
    try:
        report = planning_svc.build_planning_report(**kwargs)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    columns = list(_EXPORT_COLUMNS)
    if kwargs["include_pricing"]:
        columns += _EXPORT_PRICING_COLUMNS

    filters = {
        "planning_horizon_months": kwargs["horizon_months"],
        "historical_basis": (
            f"{kwargs['date_from']} to {kwargs['date_to']}" if kwargs["date_from"] and kwargs["date_to"]
            else f"last {kwargs['analysis_period_days']} days"
        ),
        "procurement_source_scenario": kwargs["source"],
        "search": kwargs["search"], "category_id": kwargs["category_id"],
        "machine_id": kwargs["machine_id"], "department_id": kwargs["department_id"],
        "view": kwargs["view"],
        "assumptions": (
            "Recommended purchase = projected demand (average monthly consumption x horizon) "
            "+ safety stock - current usable stock - confirmed incoming stock, clamped to zero. "
            "Incoming stock: Not tracked -- this application has no purchase-order/pending-"
            "receipt system yet, so pending purchase orders are NOT included in this plan; this "
            "is not a verified absence of incoming stock. Safety stock uses each part's "
            "configured override where set, otherwise one month of its own average "
            "consumption -- separate from, and never derived from, its Minimum Stock reorder "
            "level. Consumption is genuine Stock Out activity only; adjustments, opening "
            "balances, receipts, and voided entries are excluded. An ABC class of "
            "'Unclassified' means there is not enough pricing data to rank that item by "
            "value -- it is not a claim of low value. Confirm pricing, incoming orders, and "
            "lead times before ordering."
        ),
    }
    filters = {k: v for k, v in filters.items() if v not in (None, "", [])}

    content = build_export(
        fmt, title="Spare Parts Consumption & Procurement Plan", filters=filters,
        generated_by=current_user().username, columns=columns, rows=report["rows"],
        **branding_service.export_kwargs(),
    )
    record_audit(
        current_user(), "export", "spare_part_planning",
        after={"format": fmt, "filters": filters, "row_count": len(report["rows"])},
    )
    db.session.commit()
    return Response(content, mimetype=MIME_TYPES[fmt], headers={
        "Content-Disposition": f"attachment; filename=spare_parts_procurement_plan.{fmt}",
    })
