from flask import Blueprint, Response, jsonify, request

from webapp.auth import current_user, login_required, roles_required
from webapp.extensions import db
from webapp.models.customer import Customer
from webapp.models.dispatch import SHIFTS, STATUS_DRAFT, STATUS_FINALIZED, STATUSES, Dispatch, DispatchLine
from webapp.models.sales_category import SalesCategory
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN, User
from webapp.services import customer_service, dispatch_service as svc
from webapp.services.audit_service import record_audit
from webapp.services.dispatch_service import DispatchError
from webapp.services.export_service import MIME_TYPES, build_export
from webapp.services.packaging import PackagingError

dispatches_bp = Blueprint("dispatches", __name__, url_prefix="/api/dispatches")


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


def _filtered_dispatch_query(args):
    """Shared by the list endpoint and every export — filters always mean the same thing in both."""
    query = Dispatch.query
    filters_applied = {}

    if args.get("date"):
        query = query.filter(Dispatch.date == args["date"])
        filters_applied["date"] = args["date"]
    if args.get("date_from"):
        query = query.filter(Dispatch.date >= args["date_from"])
        filters_applied["date_from"] = args["date_from"]
    if args.get("date_to"):
        query = query.filter(Dispatch.date <= args["date_to"])
        filters_applied["date_to"] = args["date_to"]
    if args.get("shift"):
        query = query.filter(Dispatch.shift == args["shift"])
        filters_applied["shift"] = args["shift"]
    if args.get("status"):
        query = query.filter(Dispatch.status == args["status"])
        filters_applied["status"] = args["status"]
    if args.get("customer_id"):
        # Resolve merged-away recipients so filtering by the canonical
        # customer still surfaces dispatches recorded under a name later
        # merged into it — merging must never orphan history from search.
        customer_ids = customer_service.resolve_customer_ids_for_filter(int(args["customer_id"]))
        query = query.filter(Dispatch.customer_id.in_(customer_ids))
        customer = db.session.get(Customer, int(args["customer_id"]))
        filters_applied["customer"] = customer.name if customer else args["customer_id"]
    if args.get("sales_category_id"):
        query = query.filter(Dispatch.sales_category_id == int(args["sales_category_id"]))
        category = db.session.get(SalesCategory, int(args["sales_category_id"]))
        filters_applied["sales_category"] = category.name if category else args["sales_category_id"]
    if args.get("dispatch_number"):
        query = query.filter(Dispatch.dispatch_number.ilike(f"%{args['dispatch_number']}%"))
        filters_applied["dispatch_number"] = args["dispatch_number"]
    if args.get("invoice_number"):
        query = query.filter(Dispatch.invoice_number.ilike(f"%{args['invoice_number']}%"))
        filters_applied["invoice_number"] = args["invoice_number"]
    if args.get("created_by"):
        query = query.filter(Dispatch.created_by == int(args["created_by"]))
        user = db.session.get(User, int(args["created_by"]))
        filters_applied["created_by"] = user.username if user else args["created_by"]
    if args.get("product_id"):
        query = query.join(DispatchLine).filter(DispatchLine.product_id == int(args["product_id"]))
        filters_applied["product_id"] = args["product_id"]

    return query, filters_applied


@dispatches_bp.route("", methods=["GET"])
@login_required
def list_dispatches():
    query, _ = _filtered_dispatch_query(request.args)
    query = query.order_by(Dispatch.date.desc(), Dispatch.id.desc())
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    total = query.count()
    rows = query.offset(offset).limit(limit).all()

    return jsonify({
        "total": total,
        "results": [d.to_dict(include_lines=False) for d in rows],
    })


@dispatches_bp.route("/export.<fmt>", methods=["GET"])
@login_required
def export_dispatches(fmt):
    query, filters_applied = _filtered_dispatch_query(request.args)
    dispatches = query.order_by(Dispatch.date.desc(), Dispatch.id.desc()).limit(5000).all()

    columns = [
        ("date", "Date"), ("shift", "Shift"), ("dispatch_number", "Dispatch No."),
        ("invoice_number", "Invoice No."), ("sales_category", "Sales Category"),
        ("customer_name", "Customer"), ("status", "Status"),
        ("product_name", "Product"), ("cartons", "Cartons"), ("packs", "Packs"), ("pieces", "Pieces"),
        ("base_unit_qty", "Total Pieces"),
    ]
    rows = []
    total_pieces = 0
    for d in dispatches:
        customer_name = d.customer_name_snapshot or (d.customer.name if d.customer else "")
        category_name = d.sales_category_name_snapshot or "Uncategorized"
        for line in d.lines:
            rows.append({
                "date": d.date, "shift": d.shift, "dispatch_number": d.dispatch_number,
                "invoice_number": d.invoice_number or "", "sales_category": category_name,
                "customer_name": customer_name,
                "status": d.status, "product_name": line.product.name if line.product else "",
                "cartons": line.cartons, "packs": line.packs, "pieces": line.pieces,
                "base_unit_qty": line.base_unit_qty,
            })
            total_pieces += line.base_unit_qty
    totals = {"date": "", "product_name": "TOTAL", "base_unit_qty": total_pieces}

    try:
        content = build_export(fmt, title="Dispatch Transactions", filters=filters_applied,
                                generated_by=current_user().username, columns=columns, rows=rows, totals=totals)
    except ValueError as e:
        return _error(e)

    record_audit(current_user(), "export", "dispatch", after={"format": fmt, "filters": filters_applied, "row_count": len(rows)})
    db.session.commit()
    return Response(content, mimetype=MIME_TYPES[fmt],
                     headers={"Content-Disposition": f"attachment; filename=dispatch_transactions.{fmt}"})


@dispatches_bp.route("/check-number", methods=["GET"])
@login_required
def check_number():
    number = (request.args.get("number") or "").strip()
    if not number:
        return jsonify({"conflict": None})
    conflict = svc.find_duplicate_number(number)
    return jsonify({"conflict": conflict.to_dict(include_lines=False) if conflict else None})


@dispatches_bp.route("/<int:dispatch_id>", methods=["GET"])
@login_required
def get_dispatch(dispatch_id):
    dispatch = db.session.get(Dispatch, dispatch_id)
    if dispatch is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(dispatch.to_dict())


@dispatches_bp.route("", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
def create_dispatch():
    d = request.get_json(force=True) or {}
    user = current_user()

    required = ["dispatch_number", "date", "shift", "lines"]
    for f in required:
        if not d.get(f):
            return _error(f"missing field: {f}")
    if d["shift"] not in SHIFTS:
        return _error(f"shift must be one of {SHIFTS}")
    if not d.get("customer_id") and not d.get("new_customer_name"):
        return _error("either customer_id or new_customer_name is required")

    try:
        dispatch = svc.create_dispatch(
            dispatch_number=d["dispatch_number"].strip(),
            date=d["date"],
            shift=d["shift"],
            customer_id=int(d["customer_id"]) if d.get("customer_id") else None,
            new_customer_name=(d.get("new_customer_name") or "").strip() or None,
            sales_category_id=int(d["sales_category_id"]) if d.get("sales_category_id") else None,
            invoice_number=d.get("invoice_number"),
            notes=d.get("notes"),
            lines=d["lines"],
            user=user,
            override_duplicate=bool(d.get("override_duplicate")),
            override_reason=d.get("override_reason"),
        )
    except (DispatchError, PackagingError) as e:
        db.session.rollback()
        return _error(e, 409 if "already used by dispatch" in str(e) else 400)

    if d.get("new_customer_name"):
        record_audit(user, "create_temporary", "customer", entity_id=dispatch.customer_id,
                     after={"name": d["new_customer_name"], "sales_category_id": d.get("sales_category_id"),
                            "created_via": "dispatch_entry"})

    if dispatch.duplicate_override:
        record_audit(user, "duplicate_number_override", "dispatch", entity_id=dispatch.id,
                      after={"dispatch_number": dispatch.dispatch_number, "reason": dispatch.duplicate_override_reason})
    record_audit(user, "create", "dispatch", entity_id=dispatch.id, after=dispatch.to_dict())
    db.session.commit()
    return jsonify(dispatch.to_dict()), 201


def _load_editable_dispatch(dispatch_id, user):
    dispatch = db.session.get(Dispatch, dispatch_id)
    if dispatch is None:
        return None, (jsonify({"error": "not found"}), 404)
    if not svc.can_edit(dispatch, user):
        return None, (jsonify({"error": "forbidden"}), 403)
    return dispatch, None


@dispatches_bp.route("/<int:dispatch_id>", methods=["PATCH"])
@login_required
def update_dispatch(dispatch_id):
    user = current_user()
    dispatch, err = _load_editable_dispatch(dispatch_id, user)
    if err:
        return err
    if dispatch.status != STATUS_DRAFT:
        return _error("only a draft dispatch's header can be edited directly — reopen it first", 409)

    d = request.get_json(force=True) or {}
    before = dispatch.to_dict()
    changing_recipient = any(k in d for k in ("customer_id", "new_customer_name", "sales_category_id"))

    if "date" in d:
        dispatch.date = d["date"]
    if "shift" in d:
        if d["shift"] not in SHIFTS:
            return _error(f"shift must be one of {SHIFTS}")
        dispatch.shift = d["shift"]
    if "invoice_number" in d:
        dispatch.invoice_number = d["invoice_number"]
    if "notes" in d:
        dispatch.notes = d["notes"]

    if changing_recipient:
        # Recipient + category + both snapshots are updated together, in one
        # service-layer call, so there is never a partial/inconsistent state.
        try:
            svc.update_recipient(
                dispatch,
                customer_id=int(d["customer_id"]) if d.get("customer_id") else None,
                new_customer_name=(d.get("new_customer_name") or "").strip() or None,
                sales_category_id=int(d["sales_category_id"]) if d.get("sales_category_id") else None,
                user=user,
            )
        except DispatchError as e:
            db.session.rollback()
            return _error(e)
    else:
        dispatch.updated_by = user.id

    record_audit(user, "update", "dispatch", entity_id=dispatch.id, before=before, after=dispatch.to_dict())
    db.session.commit()
    return jsonify(dispatch.to_dict())


@dispatches_bp.route("/<int:dispatch_id>/lines", methods=["POST"])
@login_required
def add_line(dispatch_id):
    user = current_user()
    dispatch, err = _load_editable_dispatch(dispatch_id, user)
    if err:
        return err
    if dispatch.status != STATUS_DRAFT:
        return _error("lines can only be added to a draft dispatch — reopen it first", 409)

    d = request.get_json(force=True) or {}
    try:
        line_data = svc.build_line(
            d.get("product_id"), d.get("cartons", 0), d.get("packs", 0), d.get("pieces", 0),
            line_notes=d.get("line_notes"),
        )
    except (DispatchError, PackagingError) as e:
        return _error(e)

    line = DispatchLine(dispatch_id=dispatch.id, **line_data)
    db.session.add(line)
    dispatch.updated_by = user.id
    db.session.flush()

    record_audit(user, "add_line", "dispatch", entity_id=dispatch.id, after=line.to_dict())
    db.session.commit()
    return jsonify(dispatch.to_dict()), 201


@dispatches_bp.route("/<int:dispatch_id>/lines/<int:line_id>", methods=["PATCH"])
@login_required
def update_line(dispatch_id, line_id):
    user = current_user()
    dispatch, err = _load_editable_dispatch(dispatch_id, user)
    if err:
        return err
    if dispatch.status != STATUS_DRAFT:
        return _error("lines can only be edited on a draft dispatch — reopen it first", 409)

    line = db.session.get(DispatchLine, line_id)
    if line is None or line.dispatch_id != dispatch.id:
        return jsonify({"error": "not found"}), 404

    d = request.get_json(force=True) or {}
    before = line.to_dict()
    try:
        line_data = svc.build_line(
            d.get("product_id", line.product_id),
            d.get("cartons", line.cartons), d.get("packs", line.packs), d.get("pieces", line.pieces),
            line_notes=d.get("line_notes", line.line_notes),
        )
    except (DispatchError, PackagingError) as e:
        return _error(e)

    for key, value in line_data.items():
        setattr(line, key, value)
    dispatch.updated_by = user.id

    record_audit(user, "update_line", "dispatch", entity_id=dispatch.id, before=before, after=line.to_dict())
    db.session.commit()
    return jsonify(dispatch.to_dict())


@dispatches_bp.route("/<int:dispatch_id>/lines/<int:line_id>", methods=["DELETE"])
@login_required
def remove_line(dispatch_id, line_id):
    user = current_user()
    dispatch, err = _load_editable_dispatch(dispatch_id, user)
    if err:
        return err
    if dispatch.status != STATUS_DRAFT:
        return _error("lines can only be removed from a draft dispatch — reopen it first", 409)

    line = db.session.get(DispatchLine, line_id)
    if line is None or line.dispatch_id != dispatch.id:
        return jsonify({"error": "not found"}), 404
    if len(dispatch.lines) <= 1:
        return _error("a dispatch needs at least one product line — delete the dispatch instead by voiding it")

    before = line.to_dict()
    db.session.delete(line)
    dispatch.updated_by = user.id

    record_audit(user, "remove_line", "dispatch", entity_id=dispatch.id, before=before)
    db.session.commit()
    return jsonify(dispatch.to_dict())


@dispatches_bp.route("/<int:dispatch_id>/finalize", methods=["POST"])
@login_required
def finalize(dispatch_id):
    user = current_user()
    dispatch, err = _load_editable_dispatch(dispatch_id, user)
    if err:
        return err

    before = dispatch.to_dict()
    try:
        svc.finalize_dispatch(dispatch, user)
    except DispatchError as e:
        return _error(e)

    record_audit(user, "finalize", "dispatch", entity_id=dispatch.id, before=before, after=dispatch.to_dict())
    db.session.commit()
    return jsonify(dispatch.to_dict())


@dispatches_bp.route("/<int:dispatch_id>/reopen", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
def reopen(dispatch_id):
    user = current_user()
    dispatch = db.session.get(Dispatch, dispatch_id)
    if dispatch is None:
        return jsonify({"error": "not found"}), 404

    d = request.get_json(force=True) or {}
    before = dispatch.to_dict()
    try:
        svc.reopen_dispatch(dispatch, user, d.get("reason"))
    except DispatchError as e:
        return _error(e)

    record_audit(user, "reopen", "dispatch", entity_id=dispatch.id, before=before, after=dispatch.to_dict())
    db.session.commit()
    return jsonify(dispatch.to_dict())


@dispatches_bp.route("/<int:dispatch_id>/void", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
def void(dispatch_id):
    user = current_user()
    dispatch = db.session.get(Dispatch, dispatch_id)
    if dispatch is None:
        return jsonify({"error": "not found"}), 404

    d = request.get_json(force=True) or {}
    before = dispatch.to_dict()
    try:
        svc.void_dispatch(dispatch, user, d.get("reason"))
    except DispatchError as e:
        return _error(e)

    record_audit(user, "void", "dispatch", entity_id=dispatch.id, before=before, after=dispatch.to_dict())
    db.session.commit()
    return jsonify(dispatch.to_dict())


@dispatches_bp.route("/<int:dispatch_id>/duplicate", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
def duplicate(dispatch_id):
    user = current_user()
    source = db.session.get(Dispatch, dispatch_id)
    if source is None:
        return jsonify({"error": "not found"}), 404

    d = request.get_json(force=True) or {}
    new_number = (d.get("dispatch_number") or "").strip()
    if not new_number:
        return _error("dispatch_number is required for the new copy")

    try:
        new_dispatch = svc.duplicate_dispatch(
            source, new_number, user,
            sales_category_id=int(d["sales_category_id"]) if d.get("sales_category_id") else None,
        )
    except (DispatchError, PackagingError) as e:
        db.session.rollback()
        return _error(e)

    record_audit(user, "duplicate", "dispatch", entity_id=new_dispatch.id,
                 after={"duplicated_from": source.id, **new_dispatch.to_dict()})
    db.session.commit()
    return jsonify(new_dispatch.to_dict()), 201
