from flask import Blueprint, Response, jsonify, request
from sqlalchemy import or_

from webapp.auth import current_user, login_required, roles_required, feature_required
from webapp.extensions import db
from webapp.models.customer import Customer
from webapp.models.dispatch import STATUS_DRAFT, STATUS_FINALIZED, STATUSES, Dispatch, DispatchLine
from webapp.models.sales_category import SalesCategory
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN, User
from webapp.services import branding_service, customer_service, dispatch_service as svc
from webapp.services import product_usage_service
from webapp.services.audit_service import record_audit
from webapp.services.dispatch_service import DispatchError
from webapp.services.export_service import MIME_TYPES, build_export
from webapp.services.packaging import PackagingError
from webapp.services.quantity_format import qty_label

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
        # A correlated EXISTS, not a join — a dispatch with several lines
        # for this product (nothing in the schema forbids that; see
        # DispatchLine's model docstring) must still contribute exactly one
        # row here. A join(DispatchLine) would multiply the Dispatch row
        # once per matching line, corrupting count()/pagination/ordering and
        # causing every export format to repeat that dispatch's lines once
        # per match.
        pid = int(args["product_id"])
        query = query.filter(
            db.session.query(DispatchLine.id)
            .filter(DispatchLine.dispatch_id == Dispatch.id, DispatchLine.product_id == pid)
            .exists()
        )
        filters_applied["product_id"] = args["product_id"]
    if args.get("customer_name"):
        # Server-side, full-table search — runs before pagination, unlike the
        # old client-side substring filter that only ever saw the currently
        # loaded page. Matches against BOTH the historical/temporary-customer
        # name snapshot recorded on the dispatch (so renames, merges, and
        # long-gone temporary customers stay searchable exactly as they
        # appeared at the time) and the customer's current live name (so a
        # customer found by their present name still turns up even on older
        # dispatches whose snapshot predates a rename).
        needle = args["customer_name"].strip()
        if needle:
            query = query.outerjoin(Customer, Dispatch.customer_id == Customer.id).filter(
                or_(
                    Dispatch.customer_name_snapshot.ilike(f"%{needle}%"),
                    Customer.name.ilike(f"%{needle}%"),
                )
            )
            filters_applied["customer_name"] = needle

    return query, filters_applied


@dispatches_bp.route("", methods=["GET"])
@login_required
@feature_required("dispatch")
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
@feature_required("dispatch")
def export_dispatches(fmt):
    query, filters_applied = _filtered_dispatch_query(request.args)
    dispatches = query.order_by(Dispatch.date.desc(), Dispatch.id.desc()).limit(5000).all()

    # A single business-friendly "Quantity" column per line — cartons+packs+
    # pieces per that product's own configured packaging (e.g. "3c 2p 5pc"
    # for a product with a pack tier, "3c 5pc" for one without), the same
    # convention the app already shows on screen. This is a presentation
    # correction, not a conversion-logic rewrite: no quantity is computed
    # here, only formatted (see webapp/services/quantity_format.py).
    #
    # No raw base-unit/"total pieces" figure appears anywhere in this normal,
    # business-facing export — a mixed-product base-unit sum isn't even a
    # meaningful number (cartons of one product summed with another's), and
    # the Stage 5 correction is explicit that this never belongs in a report
    # ordinary users see. There is currently no separate admin-only
    # diagnostic export in this app; if one is ever genuinely needed, it
    # should be its own clearly-labeled endpoint, not a bonus row bolted
    # onto this one.
    columns = [
        ("date", "Date"), ("shift", "Shift"), ("dispatch_number", "Dispatch No."),
        ("invoice_number", "Invoice No."), ("sales_category", "Sales Category"),
        ("customer_name", "Customer"), ("status", "Status"),
        ("product_name", "Product"), ("quantity", "Quantity"),
    ]
    rows = []
    for d in dispatches:
        customer_name = d.customer_name_snapshot or (d.customer.name if d.customer else "")
        category_name = d.sales_category_name_snapshot or "Uncategorized"
        for line in d.lines:
            rows.append({
                "date": d.date, "shift": d.shift, "dispatch_number": d.dispatch_number,
                "invoice_number": d.invoice_number or "", "sales_category": category_name,
                "customer_name": customer_name,
                "status": d.status, "product_name": line.product.name if line.product else "",
                "quantity": qty_label(line.cartons, line.packs, line.pieces, line.packaging_rule),
            })

    try:
        content = build_export(fmt, title="Dispatch Transactions", filters=filters_applied,
                                generated_by=current_user().username, columns=columns, rows=rows,
                                **branding_service.export_kwargs())
    except ValueError as e:
        return _error(e)

    record_audit(current_user(), "export", "dispatch", after={"format": fmt, "filters": filters_applied, "row_count": len(rows)})
    db.session.commit()
    return Response(content, mimetype=MIME_TYPES[fmt],
                     headers={"Content-Disposition": f"attachment; filename=dispatch_transactions.{fmt}"})


@dispatches_bp.route("/check-number", methods=["GET"])
@login_required
@feature_required("dispatch")
def check_number():
    number = (request.args.get("number") or "").strip()
    if not number:
        return jsonify({"conflict": None})
    conflict = svc.find_duplicate_number(number)
    return jsonify({"conflict": conflict.to_dict(include_lines=False) if conflict else None})


@dispatches_bp.route("/<int:dispatch_id>", methods=["GET"])
@login_required
@feature_required("dispatch")
def get_dispatch(dispatch_id):
    dispatch = db.session.get(Dispatch, dispatch_id)
    if dispatch is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(dispatch.to_dict())


@dispatches_bp.route("", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("dispatch")
def create_dispatch():
    d = request.get_json(force=True) or {}
    user = current_user()

    # Dispatch is a Day-only workflow — no "shift" field is accepted here at
    # all (never merely defaulted while still honoring a client-supplied
    # Night). svc.create_dispatch() hardcodes Day itself.
    required = ["dispatch_number", "date", "lines"]
    for f in required:
        if not d.get(f):
            return _error(f"missing field: {f}")
    if not d.get("customer_id") and not d.get("new_customer_name"):
        return _error("either customer_id or new_customer_name is required")

    try:
        dispatch = svc.create_dispatch(
            dispatch_number=d["dispatch_number"].strip(),
            date=d["date"],
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
@feature_required("dispatch")
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
    # "shift" is intentionally not settable here — Dispatch is Day-only, and
    # a client-supplied "shift" key is silently ignored rather than honored,
    # exactly like Daily Figures now ignores a submitted "return_"/
    # "production" (see webapp/routes/daily_figures.py's upsert()).
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
@feature_required("dispatch")
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
@feature_required("dispatch")
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
@feature_required("dispatch")
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
@feature_required("dispatch")
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

    # Stage 8 Part 2 — global product quick-selection ranking: one usage
    # event per distinct product actually in this now-finalized dispatch,
    # never per line. Re-finalizing after a reopen/correction upserts the
    # same events rather than duplicating them (see
    # product_usage_service.record_usage()).
    product_usage_service.record_usage("dispatch", dispatch.id, {line.product_id for line in dispatch.lines})

    record_audit(user, "finalize", "dispatch", entity_id=dispatch.id, before=before, after=dispatch.to_dict())
    db.session.commit()
    return jsonify(dispatch.to_dict())


@dispatches_bp.route("/<int:dispatch_id>/reopen", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)
@feature_required("dispatch")
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
@feature_required("dispatch")
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

    # A voided dispatch is no longer valid completed activity — it must
    # stop contributing to the ranking (Stage 8 Part 2).
    product_usage_service.remove_usage("dispatch", dispatch.id)

    record_audit(user, "void", "dispatch", entity_id=dispatch.id, before=before, after=dispatch.to_dict())
    db.session.commit()
    return jsonify(dispatch.to_dict())


@dispatches_bp.route("/<int:dispatch_id>/duplicate", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR)
@feature_required("dispatch")
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
