from flask import Blueprint, Response, jsonify, request

from webapp.auth import current_user, feature_required, login_required, roles_required
from webapp.extensions import db
from webapp.models.dispatch import SHIFTS
from webapp.models.production_record import STATUS_DRAFT, ProductionLine, ProductionRecord
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN, User
from webapp.services import branding_service, production_service as svc
from webapp.services import product_usage_service, record_correction_service
from webapp.services.audit_service import record_audit
from webapp.services.export_service import MIME_TYPES, build_export
from webapp.services.packaging import PackagingError
from webapp.services.production_service import ProductionError
from webapp.services.quantity_format import qty_label
from webapp.services.record_correction_service import RecordCorrectionConflict, RecordCorrectionError

production_bp = Blueprint("production", __name__, url_prefix="/api/production")


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


def _filtered_production_query(args):
    """Same EXISTS-subquery-for-product_id shape as dispatches.py/returns.py —
    never a join, so a production entry with several lines for one product
    can't multiply the parent row."""
    query = ProductionRecord.query
    filters_applied = {}

    if args.get("date"):
        query = query.filter(ProductionRecord.date == args["date"])
        filters_applied["date"] = args["date"]
    if args.get("date_from"):
        query = query.filter(ProductionRecord.date >= args["date_from"])
        filters_applied["date_from"] = args["date_from"]
    if args.get("date_to"):
        query = query.filter(ProductionRecord.date <= args["date_to"])
        filters_applied["date_to"] = args["date_to"]
    if args.get("shift"):
        query = query.filter(ProductionRecord.shift == args["shift"])
        filters_applied["shift"] = args["shift"]
    if args.get("status"):
        query = query.filter(ProductionRecord.status == args["status"])
        filters_applied["status"] = args["status"]
    if args.get("created_by"):
        query = query.filter(ProductionRecord.created_by == int(args["created_by"]))
        user = db.session.get(User, int(args["created_by"]))
        filters_applied["created_by"] = user.username if user else args["created_by"]
    if args.get("product_id"):
        pid = int(args["product_id"])
        query = query.filter(
            db.session.query(ProductionLine.id)
            .filter(ProductionLine.production_id == ProductionRecord.id, ProductionLine.product_id == pid)
            .exists()
        )
        filters_applied["product_id"] = args["product_id"]

    return query, filters_applied


@production_bp.route("", methods=["GET"])
@login_required
@feature_required("production")
def list_production():
    query, _ = _filtered_production_query(request.args)
    query = query.order_by(ProductionRecord.date.desc(), ProductionRecord.id.desc())
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return jsonify({"total": total, "results": [r.to_dict(include_lines=False) for r in rows]})


@production_bp.route("/export.<fmt>", methods=["GET"])
@login_required
@feature_required("production")
def export_production(fmt):
    query, filters_applied = _filtered_production_query(request.args)
    records = query.order_by(ProductionRecord.date.desc(), ProductionRecord.id.desc()).limit(5000).all()

    columns = [
        ("date", "Date"), ("shift", "Shift"), ("status", "Status"),
        ("product_name", "Product"), ("quantity", "Quantity"), ("remarks", "Remarks"),
    ]
    rows = []
    for r in records:
        for line in r.lines:
            rows.append({
                "date": r.date, "shift": r.shift, "status": r.status,
                "product_name": line.product.name if line.product else "",
                "quantity": qty_label(line.cartons, line.packs, line.pieces, line.packaging_rule),
                "remarks": r.remarks or "",
            })

    try:
        content = build_export(fmt, title="Production", filters=filters_applied,
                                generated_by=current_user().username, columns=columns, rows=rows,
                                **branding_service.export_kwargs())
    except ValueError as e:
        return _error(e)

    record_audit(current_user(), "export", "production", after={"format": fmt, "filters": filters_applied, "row_count": len(rows)})
    db.session.commit()
    return Response(content, mimetype=MIME_TYPES[fmt],
                     headers={"Content-Disposition": f"attachment; filename=production.{fmt}"})


@production_bp.route("/<int:production_id>", methods=["GET"])
@login_required
@feature_required("production")
def get_production(production_id):
    record = db.session.get(ProductionRecord, production_id)
    if record is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(record.to_dict())


@production_bp.route("", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("production")
def create_production():
    d = request.get_json(force=True) or {}
    user = current_user()

    required = ["date", "shift", "lines"]
    for f in required:
        if not d.get(f):
            return _error(f"missing field: {f}")
    if d["shift"] not in SHIFTS:
        return _error(f"shift must be one of {SHIFTS}")

    try:
        record = svc.create_production(
            date=d["date"], shift=d["shift"], remarks=d.get("remarks"), lines=d["lines"], user=user,
        )
    except (ProductionError, PackagingError) as e:
        db.session.rollback()
        return _error(e)

    record_audit(user, "create", "production", entity_id=record.id, after=record.to_dict())
    db.session.commit()
    return jsonify(record.to_dict()), 201


def _load_editable_production(production_id, user):
    record = db.session.get(ProductionRecord, production_id)
    if record is None:
        return None, (jsonify({"error": "not found"}), 404)
    if not svc.can_edit(record, user):
        return None, (jsonify({"error": "forbidden"}), 403)
    return record, None


@production_bp.route("/<int:production_id>", methods=["PATCH"])
@login_required
@feature_required("production")
def update_production(production_id):
    user = current_user()
    record, err = _load_editable_production(production_id, user)
    if err:
        return err
    if record.status != STATUS_DRAFT:
        return _error("only a draft production entry's header can be edited directly — reopen it first", 409)

    d = request.get_json(force=True) or {}
    before = record.to_dict()
    if d.get("shift") is not None and d["shift"] not in SHIFTS:
        return _error(f"shift must be one of {SHIFTS}")
    try:
        svc.update_header(record, date=d.get("date"), shift=d.get("shift"), remarks=d.get("remarks"), user=user)
    except ProductionError as e:
        db.session.rollback()
        return _error(e)

    record_audit(user, "update", "production", entity_id=record.id, before=before, after=record.to_dict())
    db.session.commit()
    return jsonify(record.to_dict())


@production_bp.route("/<int:production_id>/lines", methods=["POST"])
@login_required
@feature_required("production")
def add_line(production_id):
    user = current_user()
    record, err = _load_editable_production(production_id, user)
    if err:
        return err
    if record.status != STATUS_DRAFT:
        return _error("lines can only be added to a draft production entry — reopen it first", 409)

    d = request.get_json(force=True) or {}
    try:
        line_data = svc.build_line(
            d.get("product_id"), d.get("cartons", 0), d.get("packs", 0), d.get("pieces", 0),
            line_notes=d.get("line_notes"),
        )
    except (ProductionError, PackagingError) as e:
        return _error(e)

    line = ProductionLine(production_id=record.id, **line_data)
    db.session.add(line)
    record.updated_by = user.id
    db.session.flush()

    record_audit(user, "add_line", "production", entity_id=record.id, after=line.to_dict())
    db.session.commit()
    return jsonify(record.to_dict()), 201


@production_bp.route("/<int:production_id>/lines/<int:line_id>", methods=["PATCH"])
@login_required
@feature_required("production")
def update_line(production_id, line_id):
    user = current_user()
    record, err = _load_editable_production(production_id, user)
    if err:
        return err
    if record.status != STATUS_DRAFT:
        return _error("lines can only be edited on a draft production entry — reopen it first", 409)

    line = db.session.get(ProductionLine, line_id)
    if line is None or line.production_id != record.id:
        return jsonify({"error": "not found"}), 404

    d = request.get_json(force=True) or {}
    before = line.to_dict()
    try:
        line_data = svc.build_line(
            d.get("product_id", line.product_id),
            d.get("cartons", line.cartons), d.get("packs", line.packs), d.get("pieces", line.pieces),
            line_notes=d.get("line_notes", line.line_notes),
        )
    except (ProductionError, PackagingError) as e:
        return _error(e)

    for key, value in line_data.items():
        setattr(line, key, value)
    record.updated_by = user.id

    record_audit(user, "update_line", "production", entity_id=record.id, before=before, after=line.to_dict())
    db.session.commit()
    return jsonify(record.to_dict())


@production_bp.route("/<int:production_id>/lines/<int:line_id>", methods=["DELETE"])
@login_required
@feature_required("production")
def remove_line(production_id, line_id):
    user = current_user()
    record, err = _load_editable_production(production_id, user)
    if err:
        return err
    if record.status != STATUS_DRAFT:
        return _error("lines can only be removed from a draft production entry — reopen it first", 409)

    line = db.session.get(ProductionLine, line_id)
    if line is None or line.production_id != record.id:
        return jsonify({"error": "not found"}), 404
    if len(record.lines) <= 1:
        return _error("a production entry needs at least one product line — delete it instead by voiding it")

    before = line.to_dict()
    db.session.delete(line)
    record.updated_by = user.id

    record_audit(user, "remove_line", "production", entity_id=record.id, before=before)
    db.session.commit()
    return jsonify(record.to_dict())


@production_bp.route("/<int:production_id>/finalize", methods=["POST"])
@login_required
@feature_required("production")
def finalize(production_id):
    user = current_user()
    record, err = _load_editable_production(production_id, user)
    if err:
        return err

    before = record.to_dict()
    try:
        svc.finalize_production(record, user)
    except ProductionError as e:
        return _error(e)

    product_usage_service.record_usage("production", record.id, {line.product_id for line in record.lines})

    record_audit(user, "finalize", "production", entity_id=record.id, before=before, after=record.to_dict())
    db.session.commit()
    return jsonify(record.to_dict())


@production_bp.route("/<int:production_id>/reopen", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("production")
def reopen(production_id):
    user = current_user()
    record = db.session.get(ProductionRecord, production_id)
    if record is None:
        return jsonify({"error": "not found"}), 404

    d = request.get_json(force=True) or {}
    before = record.to_dict()
    try:
        svc.reopen_production(record, user, d.get("reason"))
    except ProductionError as e:
        return _error(e)

    record_audit(user, "reopen", "production", entity_id=record.id, before=before, after=record.to_dict())
    db.session.commit()
    return jsonify(record.to_dict())


@production_bp.route("/<int:production_id>/void", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("production")
def void(production_id):
    """
    Manager/Super Admin may void any production entry, any day. An
    Operator may directly void their own production entry too —
    unconditional on record age; see dispatches.py's void() for the full
    rationale (identical rule, same operator_can_directly_void() check).
    There is no "Request Void".
    """
    user = current_user()
    record = db.session.get(ProductionRecord, production_id)
    if record is None:
        return jsonify({"error": "not found"}), 404
    if user.role == ROLE_OPERATOR and not record_correction_service.operator_can_directly_void(record, user):
        return jsonify({"error": "forbidden — you may only void your own record"}), 403

    d = request.get_json(force=True) or {}
    before = record.to_dict()
    try:
        svc.void_production(record, user, d.get("reason"))
    except ProductionError as e:
        return _error(e)

    product_usage_service.remove_usage("production", record.id)

    record_audit(user, "void", "production", entity_id=record.id, before=before, after=record.to_dict())
    db.session.commit()
    return jsonify(record.to_dict())


@production_bp.route("/<int:production_id>", methods=["DELETE"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("production")
def delete_production(production_id):
    """
    Permanent hard delete — not void, not a status change. Manager/Super
    Administrator only. Mirrors dispatches.py's delete_dispatch() route
    exactly — see webapp/services/production_service.py's
    delete_production() docstring for why removal alone is enough to
    correct every live calculation.
    """
    user = current_user()
    record = db.session.get(ProductionRecord, production_id)
    if record is None:
        return jsonify({"error": "not found"}), 404

    d = request.get_json(force=True) or {}
    reason = (d.get("reason") or "").strip()
    if not reason:
        return _error("A reason is required to permanently delete a production entry")
    if not d.get("confirm"):
        return _error("Explicit confirmation is required to permanently delete a production entry")

    snapshot = {
        **record.to_dict(),
        "operation": "permanent_delete_production",
        "deletion_reason": reason,
        "previous_status": record.status,
    }

    try:
        product_usage_service.remove_usage("production", record.id)
        svc.delete_production(record, reason)
    except ProductionError as e:
        db.session.rollback()
        return _error(e)

    record_audit(user, "permanent_delete_production", "production", entity_id=production_id, before=snapshot, after=None)
    db.session.commit()
    return jsonify({"ok": True, "deleted_id": production_id})


@production_bp.route("/<int:production_id>/correct", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("production")
def correct(production_id):
    """Final pre-deployment correction — "Correct Record": see
    webapp/services/record_correction_service.py.

    Manager/Super Admin may correct any production entry, draft or
    finalized, any day. An Operator may correct one too, via a direct
    24-hour edit window or an active approval grant — see dispatches.py's
    correct() for the full rationale (identical rule).
    """
    from webapp.services import correction_request_service

    user = current_user()
    existing = record_correction_service.get_record("production", production_id)
    if existing is None:
        return jsonify({"error": "not found"}), 404

    grant = None
    if user.role == ROLE_OPERATOR and not record_correction_service.operator_can_directly_edit(existing, user):
        grant = correction_request_service.get_active_grant("production", production_id, user)
        if grant is None:
            return jsonify({
                "error": "forbidden — this record's 24-hour edit window has closed; submit a Request Correction instead",
            }), 403
        try:
            correction_request_service.consume_grant(grant)
        except correction_request_service.CorrectionRequestError as e:
            db.session.rollback()
            return _error(e, 409)

    d = request.get_json(force=True) or {}
    try:
        record, summary = record_correction_service.correct_record(
            "production", production_id,
            lines=d.get("lines"), notes=d.get("notes"), reason=d.get("reason"),
            actor=user, expected_updated_at=d.get("expected_updated_at"),
            date=d.get("date"), shift=d.get("shift"),
            via_request_id=grant.id if grant else None,
        )
    except RecordCorrectionConflict as e:
        db.session.rollback()
        return _error(e, 409)
    except (RecordCorrectionError, ProductionError, PackagingError) as e:
        db.session.rollback()
        return _error(e)

    db.session.commit()
    return jsonify({"production": record.to_dict(), "correction": summary})


@production_bp.route("/<int:production_id>/audit-history", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("production")
def audit_history(production_id):
    record = db.session.get(ProductionRecord, production_id)
    if record is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(record_correction_service.audit_history("production", production_id))
