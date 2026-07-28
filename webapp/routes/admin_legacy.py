from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from webapp.auth import current_user, roles_required
from webapp.extensions import db
from webapp.models.daily_figure import LegacyMigrationFlag
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services.audit_service import record_audit
from webapp.services.legacy_migration import run_legacy_migration

admin_legacy_bp = Blueprint("admin_legacy", __name__, url_prefix="/api/admin/legacy")


@admin_legacy_bp.route("/migrate", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN)
def migrate():
    """
    Deliberately manual and re-runnable: converts legacy entries rows into
    the new daily_figures/stock_adjustments structure, product by product,
    flagging anything that can't be confidently decoded. Never touches the
    original entries table.
    """
    user = current_user()
    summary = run_legacy_migration(user)
    record_audit(user, "run_legacy_migration", "daily_figure", after=summary)
    db.session.commit()
    return jsonify(summary)


@admin_legacy_bp.route("/flags", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN)
def list_flags():
    query = LegacyMigrationFlag.query
    if request.args.get("resolved") is not None:
        query = query.filter(LegacyMigrationFlag.resolved == (request.args["resolved"] == "1"))
    rows = query.order_by(LegacyMigrationFlag.date, LegacyMigrationFlag.shift).limit(500).all()
    return jsonify([f.to_dict() for f in rows])


@admin_legacy_bp.route("/flags/<int:flag_id>", methods=["PATCH"])
@roles_required(ROLE_SUPER_ADMIN)
def resolve_flag(flag_id):
    flag = db.session.get(LegacyMigrationFlag, flag_id)
    if flag is None:
        return jsonify({"error": "not found"}), 404
    user = current_user()
    before = flag.to_dict()
    flag.resolved = True
    flag.resolved_by = user.id
    flag.resolved_at = datetime.now(timezone.utc).replace(tzinfo=None)

    record_audit(user, "resolve", "legacy_migration_flag", entity_id=flag.id, before=before, after=flag.to_dict())
    db.session.commit()
    return jsonify(flag.to_dict())
