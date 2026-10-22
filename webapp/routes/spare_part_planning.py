"""
Consumption & Procurement Planning — read-only analytics on top of the
Spare Parts master/ledger data. The report mixes operational columns
(consumption, trend, coverage) with procurement/sourcing/value-derived
data (preferred/selected source, ABC class, projected-cost and savings
summary keys) that is unsafe to sanitize field-by-field, so the whole
endpoint — report and exports alike — is Super-Administrator ONLY, same
as the page itself (see webapp/routes/pages.py's spare_parts_planning_page()
and nav gating in static/spare-parts-shell.js). _may_see_pricing() is kept
only as an extra belt-and-braces gate on the money fields within the
response the Super Administrator receives.

Marking a part Critical reuses the EXISTING PATCH /api/spare-parts/<id>
endpoint (webapp/services/spare_part_service.py's update_spare_part()
already accepts critical/critical_note) rather than a parallel endpoint.
Unlike most other spare-part master-data edits, changing Critical status
is Super-Administrator-only (see that route's _critical_forbidden_response())
— it drives procurement priority directly, so it is restricted the same
way pricing is, even though Manager may still edit this same spare part's
other fields. Already audited by that route's existing before/after diff.
"""
from decimal import Decimal

from flask import Blueprint, Response, jsonify, request

from webapp.auth import current_user, roles_required
from webapp.models.machine import Machine
from webapp.models.spare_part import SparePart
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services import branding_service
from webapp.services import spare_part_analytics_service as analytics
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
@roles_required(ROLE_SUPER_ADMIN)
def planning_report():
    try:
        report = planning_svc.build_planning_report(**_report_kwargs())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(report)


@spare_part_planning_bp.route("/replacement-estimates", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN)
def replacement_estimates():
    """
    Machine Replacement Estimates ("Estimated Working Period"): how long a
    spare part typically runs on a specific machine before it is issued
    to that machine again, inferred from repeat Stock Out dates — never
    machine/spare "fits" master data, and never mixed across machines, so
    two machines running the same part are never averaged together as if
    they were one operating environment.
    """
    machine_id = _parse_int(request.args.get("machine_id"), None)
    spare_part_id = _parse_int(request.args.get("spare_part_id"), None)
    if machine_id is None:
        return jsonify({
            "results": [],
            "guidance": "Select a machine to view replacement interval estimates.",
        })

    estimates = analytics.replacement_estimates(spare_part_id=spare_part_id, machine_id=machine_id)
    part_ids = {e.spare_part_id for e in estimates}
    machine_ids = {e.machine_id for e in estimates}
    parts = {p.id: p for p in SparePart.query.filter(SparePart.id.in_(part_ids)).all()}
    machine_names = {m.id: m.name for m in Machine.query.filter(Machine.id.in_(machine_ids)).all()}

    rows = []
    for e in estimates:
        part = parts.get(e.spare_part_id)
        if part is None:
            continue
        rows.append({
            "spare_part_id": e.spare_part_id, "name": part.name, "specifications": part.specifications,
            "display_name": part.display_name, "unit": part.unit,
            "machine_id": e.machine_id, "machine_name": machine_names.get(e.machine_id, ""),
            "issue_event_count": e.issue_event_count, "interval_count": e.interval_count,
            "average_interval_days": (
                str(e.average_interval_days.quantize(Decimal("0.1"))) if e.average_interval_days is not None else None
            ),
            "last_issue_date": e.last_issue_date, "estimated_next_need_date": e.estimated_next_need_date,
            "evidence_label": e.evidence_label,
        })
    rows.sort(key=lambda r: (
        r["estimated_next_need_date"] is None,
        r["estimated_next_need_date"] or "",
        r["display_name"].lower(),
    ))
    return jsonify({
        "results": rows,
        "methodology": (
            "Based on all recorded machine issue history for each spare part + machine pair — "
            "an estimate inferred from repeat Stock Out issue dates, not a measured component "
            "lifespan. Not limited by Planning's historical analysis period control."
        ),
    })


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
@roles_required(ROLE_SUPER_ADMIN)
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
