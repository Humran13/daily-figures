"""
Preview endpoints (GET) are read-only with respect to recipient data: they
never create, update, or delete a single Customer row. The ONLY write a
preview performs is one audit-log entry recording that a preview was
viewed (who, when) — no recipient data is touched. Execute endpoints
(POST) are the sole write path, and additionally require an explicit
`{"confirm": true}` in the request body — this is enforced here on the
backend, not just as a frontend confirm() dialog, so the import can never
run by accident (e.g. a stray retried request, a script that only checked
the HTTP method).
"""
from flask import Blueprint, jsonify, request

from webapp.auth import current_user, roles_required, feature_required
from webapp.extensions import db
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services.audit_service import record_audit
from webapp.services.recipient_import_service import (
    CORPORATE_SALES_NAMES,
    INITIAL_ASSIGNMENTS,
    RecipientImportError,
    execute_batch,
    preview_batch,
)

admin_recipient_import_bp = Blueprint(
    "admin_recipient_import", __name__, url_prefix="/api/admin/recipient-import"
)


def _error(e, status=400):
    return jsonify({"error": str(e)}), status


def _require_confirmation():
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return _error(
            "explicit confirmation is required to execute an import — "
            "resend with {\"confirm\": true} once you have reviewed the preview",
            status=400,
        )
    return None


def _grouped_by_category(pairs):
    """[(name, category), ...] -> {category: [names]}"""
    grouped = {}
    for name, category in pairs:
        grouped.setdefault(category, []).append(name)
    return grouped


@admin_recipient_import_bp.route("/initial-assignments/preview", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN)
@feature_required("customer_management")
def preview_initial_assignments():
    """Read-only: computes what WOULD be created, creates nothing."""
    results = []
    for category, names in _grouped_by_category(INITIAL_ASSIGNMENTS).items():
        try:
            results.append(preview_batch(names, category))
        except RecipientImportError as e:
            return _error(e)
    # The only write a preview performs: one audit record that a preview
    # was viewed. No customer row is created, updated, or deleted here.
    record_audit(current_user(), "import_preview", "customer", after={"batch": "initial_assignments"})
    db.session.commit()
    return jsonify(results)


@admin_recipient_import_bp.route("/initial-assignments/execute", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN)
@feature_required("customer_management")
def execute_initial_assignments():
    err = _require_confirmation()
    if err:
        return err

    results = []
    try:
        for category, names in _grouped_by_category(INITIAL_ASSIGNMENTS).items():
            results.append(execute_batch(names, category, current_user()))
    except RecipientImportError as e:
        db.session.rollback()
        return _error(e)

    record_audit(current_user(), "import_execute", "customer", after={"batch": "initial_assignments", "results": results})
    db.session.commit()
    return jsonify(results), 201


@admin_recipient_import_bp.route("/corporate-sales/preview", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN)
@feature_required("customer_management")
def preview_corporate_sales():
    """Read-only: computes what WOULD be created, creates nothing."""
    try:
        result = preview_batch(CORPORATE_SALES_NAMES, "Corporate Sales")
    except RecipientImportError as e:
        return _error(e)
    # The only write a preview performs: one audit record that a preview
    # was viewed. No customer row is created, updated, or deleted here.
    record_audit(current_user(), "import_preview", "customer", after={"batch": "corporate_sales"})
    db.session.commit()
    return jsonify(result)


@admin_recipient_import_bp.route("/corporate-sales/execute", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN)
@feature_required("customer_management")
def execute_corporate_sales():
    err = _require_confirmation()
    if err:
        return err

    try:
        result = execute_batch(CORPORATE_SALES_NAMES, "Corporate Sales", current_user())
    except RecipientImportError as e:
        db.session.rollback()
        return _error(e)

    record_audit(current_user(), "import_execute", "customer", after={"batch": "corporate_sales", **result})
    db.session.commit()
    return jsonify(result), 201
