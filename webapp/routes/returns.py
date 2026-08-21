from flask import Blueprint, Response, jsonify, request
from sqlalchemy import or_

from webapp.auth import current_user, feature_required, login_required, roles_required
from webapp.extensions import db
from webapp.models.customer import Customer
from webapp.models.return_record import STATUS_DRAFT, STATUSES, ReturnLine, ReturnRecord
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN, User
from webapp.services import branding_service, customer_service, returns_service as svc
from webapp.services import product_usage_service, record_correction_service
from webapp.services.audit_service import record_audit
from webapp.services.export_service import MIME_TYPES, build_export
from webapp.services.packaging import PackagingError
from webapp.services.quantity_format import qty_label
from webapp.services.record_correction_service import RecordCorrectionConflict, RecordCorrectionError
from webapp.services.returns_service import ReturnError

returns_bp = Blueprint("returns", __name__, url_prefix="/api/returns")


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


def _serialize_return(record, include_lines=True):
    """
    record.to_dict() plus the two Metro Sales stock-posting flags — computed
    here (never inside the model, which would need its own copy of the
    Monday/Metro rule) via the one central svc.is_return_stock_posting_
    eligible()/svc.is_metro_sales_return() functions, so the frontend can
    show the right read-only indicator without deciding the rule itself.
    """
    d = record.to_dict(include_lines=include_lines)
    d["stock_posting_eligible"] = svc.is_return_stock_posting_eligible(record)
    d["is_metro_sales_return"] = svc.is_metro_sales_return(record)
    return d


def _filtered_return_query(args):
    """Same shape as dispatches.py's _filtered_dispatch_query — an EXISTS
    subquery (not a join) for product_id, so a return with several lines
    for one product never multiplies the parent row (see the Stage 5
    spec's History section, which calls out exactly this class of bug)."""
    query = ReturnRecord.query
    filters_applied = {}

    if args.get("date"):
        query = query.filter(ReturnRecord.date == args["date"])
        filters_applied["date"] = args["date"]
    if args.get("date_from"):
        query = query.filter(ReturnRecord.date >= args["date_from"])
        filters_applied["date_from"] = args["date_from"]
    if args.get("date_to"):
        query = query.filter(ReturnRecord.date <= args["date_to"])
        filters_applied["date_to"] = args["date_to"]
    if args.get("status"):
        query = query.filter(ReturnRecord.status == args["status"])
        filters_applied["status"] = args["status"]
    if args.get("created_by"):
        query = query.filter(ReturnRecord.created_by == int(args["created_by"]))
        user = db.session.get(User, int(args["created_by"]))
        filters_applied["created_by"] = user.username if user else args["created_by"]
    if args.get("customer_id"):
        customer_ids = customer_service.resolve_customer_ids_for_filter(int(args["customer_id"]))
        query = query.filter(ReturnRecord.returned_by_customer_id.in_(customer_ids))
        customer = db.session.get(Customer, int(args["customer_id"]))
        filters_applied["customer"] = customer.name if customer else args["customer_id"]
    if args.get("product_id"):
        pid = int(args["product_id"])
        query = query.filter(
            db.session.query(ReturnLine.id)
            .filter(ReturnLine.return_id == ReturnRecord.id, ReturnLine.product_id == pid)
            .exists()
        )
        filters_applied["product_id"] = args["product_id"]
    if args.get("returned_by"):
        needle = args["returned_by"].strip()
        if needle:
            query = query.outerjoin(Customer, ReturnRecord.returned_by_customer_id == Customer.id).filter(
                or_(
                    ReturnRecord.returned_by_name_snapshot.ilike(f"%{needle}%"),
                    Customer.name.ilike(f"%{needle}%"),
                )
            )
            filters_applied["returned_by"] = needle

    return query, filters_applied


@returns_bp.route("", methods=["GET"])
@login_required
@feature_required("returns")
def list_returns():
    query, _ = _filtered_return_query(request.args)
    query = query.order_by(ReturnRecord.date.desc(), ReturnRecord.id.desc())
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return jsonify({"total": total, "results": [_serialize_return(r, include_lines=False) for r in rows]})


@returns_bp.route("/export.<fmt>", methods=["GET"])
@login_required
@feature_required("returns")
def export_returns(fmt):
    query, filters_applied = _filtered_return_query(request.args)
    records = query.order_by(ReturnRecord.date.desc(), ReturnRecord.id.desc()).limit(5000).all()

    # Simplified business-facing shape: Returned By (who brought the goods
    # back) / Received By (the staff who received it — reuses the existing
    # signed_by_name field, previously labeled "Name & Sign"; see
    # returns_service._resolve_signed_by_name()). The old received_by/
    # verified_by integer-user-id columns are no longer printed/exported —
    # they remain untouched in the database as internal audit fields (see
    # ReturnRecord.received_by/verified_by), just no longer duplicated
    # here alongside Received By.
    columns = [
        ("date", "Date"), ("returned_by", "Returned By"), ("received_by", "Received By"),
        ("status", "Status"), ("product_name", "Product"), ("quantity", "Quantity"),
        ("posted_to_stock", "Posted to Stock"), ("remarks", "Remarks"),
    ]
    rows = []
    for r in records:
        returned_by = r.returned_by_name_snapshot or (r.returned_by_customer.name if r.returned_by_customer else "")
        # Every row is still exported — this is the same "record activity"
        # export it always was — Posted to Stock just makes the Metro
        # Sales Monday-only distinction visible (svc.is_return_stock_
        # posting_eligible() is THE central rule, never re-derived here).
        posted_to_stock = "Yes" if svc.is_return_stock_posting_eligible(r) else "No"
        for line in r.lines:
            rows.append({
                "date": r.date, "returned_by": returned_by,
                "received_by": r.signed_by_name or "",
                "status": r.status, "product_name": line.product.name if line.product else "",
                "quantity": qty_label(line.cartons, line.packs, line.pieces, line.packaging_rule),
                "posted_to_stock": posted_to_stock,
                "remarks": r.remarks or "",
            })

    try:
        content = build_export(fmt, title="Returns", filters=filters_applied,
                                generated_by=current_user().username, columns=columns, rows=rows,
                                **branding_service.export_kwargs())
    except ValueError as e:
        return _error(e)

    record_audit(current_user(), "export", "return", after={"format": fmt, "filters": filters_applied, "row_count": len(rows)})
    db.session.commit()
    return Response(content, mimetype=MIME_TYPES[fmt],
                     headers={"Content-Disposition": f"attachment; filename=returns.{fmt}"})


@returns_bp.route("/<int:return_id>", methods=["GET"])
@login_required
@feature_required("returns")
def get_return(return_id):
    record = db.session.get(ReturnRecord, return_id)
    if record is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(_serialize_return(record))


@returns_bp.route("", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("returns")
def create_return():
    d = request.get_json(force=True) or {}
    user = current_user()

    if not d.get("date"):
        return _error("missing field: date")
    if not d.get("lines"):
        return _error("missing field: lines")

    try:
        record = svc.create_return(
            date=d["date"],
            returned_by_customer_id=int(d["customer_id"]) if d.get("customer_id") else None,
            returned_by_name=d.get("returned_by_name"),
            signed_by_name=d.get("signed_by_name"),
            received_by=int(d["received_by"]) if d.get("received_by") else None,
            verified_by=int(d["verified_by"]) if d.get("verified_by") else None,
            remarks=d.get("remarks"),
            lines=d["lines"],
            user=user,
        )
    except (ReturnError, PackagingError) as e:
        db.session.rollback()
        return _error(e, 409 if "already exists for" in str(e) else 400)

    record_audit(user, "create", "return", entity_id=record.id, after=record.to_dict())
    db.session.commit()
    return jsonify(record.to_dict()), 201


def _load_editable_return(return_id, user):
    record = db.session.get(ReturnRecord, return_id)
    if record is None:
        return None, (jsonify({"error": "not found"}), 404)
    if not svc.can_edit(record, user):
        return None, (jsonify({"error": "forbidden"}), 403)
    return record, None


@returns_bp.route("/<int:return_id>", methods=["PATCH"])
@login_required
@feature_required("returns")
def update_return(return_id):
    user = current_user()
    record, err = _load_editable_return(return_id, user)
    if err:
        return err
    if record.status != STATUS_DRAFT:
        return _error("only a draft return's header can be edited directly — reopen it first", 409)

    d = request.get_json(force=True) or {}
    before = record.to_dict()
    try:
        svc.update_header(
            record,
            date=d.get("date"),
            returned_by_customer_id=int(d["customer_id"]) if d.get("customer_id") else (None if "customer_id" in d else None),
            returned_by_name=d.get("returned_by_name"),
            signed_by_name=d.get("signed_by_name"),
            received_by=int(d["received_by"]) if d.get("received_by") else (None if "received_by" in d else None),
            verified_by=int(d["verified_by"]) if d.get("verified_by") else (None if "verified_by" in d else None),
            remarks=d.get("remarks"),
            user=user,
        )
    except ReturnError as e:
        db.session.rollback()
        return _error(e, 409 if "already exists for" in str(e) else 400)

    record_audit(user, "update", "return", entity_id=record.id, before=before, after=record.to_dict())
    db.session.commit()
    return jsonify(record.to_dict())


@returns_bp.route("/<int:return_id>/lines", methods=["POST"])
@login_required
@feature_required("returns")
def add_line(return_id):
    user = current_user()
    record, err = _load_editable_return(return_id, user)
    if err:
        return err
    if record.status != STATUS_DRAFT:
        return _error("lines can only be added to a draft return — reopen it first", 409)

    d = request.get_json(force=True) or {}
    try:
        line_data = svc.build_line(
            d.get("product_id"), d.get("cartons", 0), d.get("packs", 0), d.get("pieces", 0),
            line_notes=d.get("line_notes"),
        )
    except (ReturnError, PackagingError) as e:
        return _error(e)

    line = ReturnLine(return_id=record.id, **line_data)
    db.session.add(line)
    record.updated_by = user.id
    db.session.flush()

    record_audit(user, "add_line", "return", entity_id=record.id, after=line.to_dict())
    db.session.commit()
    return jsonify(record.to_dict()), 201


@returns_bp.route("/<int:return_id>/lines/<int:line_id>", methods=["PATCH"])
@login_required
@feature_required("returns")
def update_line(return_id, line_id):
    user = current_user()
    record, err = _load_editable_return(return_id, user)
    if err:
        return err
    if record.status != STATUS_DRAFT:
        return _error("lines can only be edited on a draft return — reopen it first", 409)

    line = db.session.get(ReturnLine, line_id)
    if line is None or line.return_id != record.id:
        return jsonify({"error": "not found"}), 404

    d = request.get_json(force=True) or {}
    before = line.to_dict()
    try:
        line_data = svc.build_line(
            d.get("product_id", line.product_id),
            d.get("cartons", line.cartons), d.get("packs", line.packs), d.get("pieces", line.pieces),
            line_notes=d.get("line_notes", line.line_notes),
        )
    except (ReturnError, PackagingError) as e:
        return _error(e)

    for key, value in line_data.items():
        setattr(line, key, value)
    record.updated_by = user.id

    record_audit(user, "update_line", "return", entity_id=record.id, before=before, after=line.to_dict())
    db.session.commit()
    return jsonify(record.to_dict())


@returns_bp.route("/<int:return_id>/lines/<int:line_id>", methods=["DELETE"])
@login_required
@feature_required("returns")
def remove_line(return_id, line_id):
    user = current_user()
    record, err = _load_editable_return(return_id, user)
    if err:
        return err
    if record.status != STATUS_DRAFT:
        return _error("lines can only be removed from a draft return — reopen it first", 409)

    line = db.session.get(ReturnLine, line_id)
    if line is None or line.return_id != record.id:
        return jsonify({"error": "not found"}), 404
    if len(record.lines) <= 1:
        return _error("a return needs at least one product line — delete the return instead by voiding it")

    before = line.to_dict()
    db.session.delete(line)
    record.updated_by = user.id

    record_audit(user, "remove_line", "return", entity_id=record.id, before=before)
    db.session.commit()
    return jsonify(record.to_dict())


@returns_bp.route("/<int:return_id>/finalize", methods=["POST"])
@login_required
@feature_required("returns")
def finalize(return_id):
    user = current_user()
    record, err = _load_editable_return(return_id, user)
    if err:
        return err

    before = record.to_dict()
    try:
        svc.finalize_return(record, user)
    except ReturnError as e:
        return _error(e)

    product_usage_service.record_usage("returns", record.id, {line.product_id for line in record.lines})

    record_audit(user, "finalize", "return", entity_id=record.id, before=before, after=record.to_dict())
    db.session.commit()
    return jsonify(record.to_dict())


@returns_bp.route("/<int:return_id>/reopen", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("returns")
def reopen(return_id):
    user = current_user()
    record = db.session.get(ReturnRecord, return_id)
    if record is None:
        return jsonify({"error": "not found"}), 404

    d = request.get_json(force=True) or {}
    before = record.to_dict()
    try:
        svc.reopen_return(record, user, d.get("reason"))
    except ReturnError as e:
        return _error(e)

    record_audit(user, "reopen", "return", entity_id=record.id, before=before, after=record.to_dict())
    db.session.commit()
    return jsonify(record.to_dict())


@returns_bp.route("/<int:return_id>/void", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("returns")
def void(return_id):
    """
    Manager/Super Admin may void any return, any day. An Operator may
    directly void their own return too — unconditional on record age; see
    dispatches.py's void() for the full rationale (identical rule, same
    operator_can_directly_void() check). There is no "Request Void".
    """
    user = current_user()
    record = db.session.get(ReturnRecord, return_id)
    if record is None:
        return jsonify({"error": "not found"}), 404
    if user.role == ROLE_OPERATOR and not record_correction_service.operator_can_directly_void(record, user):
        return jsonify({"error": "forbidden — you may only void your own record"}), 403

    d = request.get_json(force=True) or {}
    before = record.to_dict()
    try:
        svc.void_return(record, user, d.get("reason"))
    except ReturnError as e:
        return _error(e)

    product_usage_service.remove_usage("returns", record.id)

    record_audit(user, "void", "return", entity_id=record.id, before=before, after=record.to_dict())
    db.session.commit()
    return jsonify(record.to_dict())


@returns_bp.route("/<int:return_id>", methods=["DELETE"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("returns")
def delete_return(return_id):
    """
    Permanent hard delete — not void, not a status change. Manager/Super
    Administrator only. Mirrors dispatches.py's delete_dispatch() route
    exactly — see webapp/services/returns_service.py's delete_return()
    docstring for why removal alone is enough to correct every live
    calculation.
    """
    user = current_user()
    record = db.session.get(ReturnRecord, return_id)
    if record is None:
        return jsonify({"error": "not found"}), 404

    d = request.get_json(force=True) or {}
    reason = (d.get("reason") or "").strip()
    if not reason:
        return _error("A reason is required to permanently delete a return")
    if not d.get("confirm"):
        return _error("Explicit confirmation is required to permanently delete a return")

    snapshot = {
        **record.to_dict(),
        "operation": "permanent_delete_return",
        "deletion_reason": reason,
        "previous_status": record.status,
    }

    try:
        product_usage_service.remove_usage("returns", record.id)
        svc.delete_return(record, reason)
    except ReturnError as e:
        db.session.rollback()
        return _error(e)

    record_audit(user, "permanent_delete_return", "return", entity_id=return_id, before=snapshot, after=None)
    db.session.commit()
    return jsonify({"ok": True, "deleted_id": return_id})


@returns_bp.route("/<int:return_id>/correct", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("returns")
def correct(return_id):
    """Final pre-deployment correction — "Correct Record": see
    webapp/services/record_correction_service.py.

    Manager/Super Admin may correct any return, draft or finalized, any
    day. An Operator may correct one too, via a direct 24-hour edit
    window or an active approval grant — see dispatches.py's correct()
    for the full rationale (identical rule).
    """
    from webapp.services import correction_request_service

    user = current_user()
    existing = record_correction_service.get_record("returns", return_id)
    if existing is None:
        return jsonify({"error": "not found"}), 404

    grant = None
    if user.role == ROLE_OPERATOR and not record_correction_service.operator_can_directly_edit(existing, user):
        grant = correction_request_service.get_active_grant("returns", return_id, user)
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
            "returns", return_id,
            lines=d.get("lines"), notes=d.get("notes"), reason=d.get("reason"),
            actor=user, expected_updated_at=d.get("expected_updated_at"),
            date=d.get("date"),
            returned_by_customer_id=int(d["customer_id"]) if d.get("customer_id") else None,
            returned_by_name=d.get("returned_by_name"),
            signed_by_name=d.get("signed_by_name"),
            via_request_id=grant.id if grant else None,
        )
    except RecordCorrectionConflict as e:
        db.session.rollback()
        return _error(e, 409)
    except (RecordCorrectionError, ReturnError, PackagingError) as e:
        db.session.rollback()
        return _error(e, 409 if "already exists for" in str(e) else 400)

    db.session.commit()
    return jsonify({"return": record.to_dict(), "correction": summary})


@returns_bp.route("/<int:return_id>/audit-history", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("returns")
def audit_history(return_id):
    record = db.session.get(ReturnRecord, return_id)
    if record is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(record_correction_service.audit_history("returns", return_id))
