"""
Spare Part Fabrication — FINANCIAL DATA, Super Administrator only, end to
end. Every route below is independently @roles_required(ROLE_SUPER_ADMIN)
— never merely @login_required — because the Master Data page being
Super-Admin-gated is a frontend convenience, not the actual security
boundary (see webapp/services/spare_part_fabrication_service.py's module
docstring).
"""
import logging
import os

from flask import Blueprint, abort, jsonify, request, send_file
from sqlalchemy.exc import SQLAlchemyError

from webapp.auth import current_user, roles_required
from webapp.extensions import db
from webapp.models.spare_part import SparePart
from webapp.models.spare_part_fabrication import SparePartFabrication
from webapp.models.user import ROLE_SUPER_ADMIN
from webapp.services import spare_part_fabrication_service as svc

logger = logging.getLogger(__name__)

spare_part_fabrications_bp = Blueprint(
    "spare_part_fabrications", __name__, url_prefix="/api/spare-parts/fabrications",
)

# Generic, safe failure message for any create/update commit failure —
# never SQL text, filenames, filesystem paths, or a traceback. The real
# exception is logged server-side by svc.finalize_write().
_SAVE_FAILED_MSG = "Could not save fabrication. Nothing was changed; please try again."
_DELETE_FAILED_MSG = "Delete failed. Nothing was removed; please try again."


def _enrich(fabrication, parts):
    row = fabrication.to_dict()
    part = parts.get(fabrication.spare_part_id)
    row["spare_part_name"] = part.name if part else None
    row["specifications"] = part.specifications if part else None
    row["display_name"] = part.display_name if part else None
    row["unit"] = part.unit if part else None
    return row


@spare_part_fabrications_bp.route("", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN)
def list_fabrications():
    rows = svc.list_fabrications(
        search=request.args.get("q"),
        date_from=request.args.get("date_from") or None,
        date_to=request.args.get("date_to") or None,
    )
    part_ids = {r.spare_part_id for r in rows}
    parts = {p.id: p for p in SparePart.query.filter(SparePart.id.in_(part_ids)).all()}
    return jsonify([_enrich(r, parts) for r in rows])


@spare_part_fabrications_bp.route("", methods=["POST"])
@roles_required(ROLE_SUPER_ADMIN)
def create_fabrication():
    form = request.form
    try:
        result = svc.create_fabrication(
            spare_part_id=form.get("spare_part_id", type=int),
            fabrication_date=form.get("fabrication_date"),
            fabrication_cost_ugx=form.get("fabrication_cost_ugx"),
            sold_to_factory_ugx=form.get("sold_to_factory_ugx"),
            notes=form.get("notes"),
            user=current_user(),
            image_file_storage=request.files.get("image"),
        )
    except svc.FabricationError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("flush failed while creating a spare part fabrication")
        return jsonify({"error": _SAVE_FAILED_MSG}), 500

    # Commit + file cleanup are sequenced together here: a failed commit
    # removes only the image this request itself just wrote (nothing else
    # could be referencing it yet) and creates no DB record; a successful
    # commit keeps it. See spare_part_fabrication_service.finalize_write().
    if not svc.finalize_write(result):
        return jsonify({"error": _SAVE_FAILED_MSG}), 500

    fabrication = result.fabrication
    part = db.session.get(SparePart, fabrication.spare_part_id)
    return jsonify(_enrich(fabrication, {part.id: part} if part else {})), 201


@spare_part_fabrications_bp.route("/<int:fabrication_id>", methods=["PATCH"])
@roles_required(ROLE_SUPER_ADMIN)
def update_fabrication(fabrication_id):
    fabrication = db.session.get(SparePartFabrication, fabrication_id)
    if fabrication is None:
        return jsonify({"error": "not found"}), 404
    form = request.form
    changes = {}
    for field in ("spare_part_id", "fabrication_date", "fabrication_cost_ugx", "sold_to_factory_ugx", "notes"):
        if field in form:
            changes[field] = form.get(field, type=int) if field == "spare_part_id" else form.get(field)
    remove_image = form.get("remove_image") in ("1", "true", "True")
    try:
        result = svc.update_fabrication(
            fabrication, changes, current_user(),
            image_file_storage=request.files.get("image"), remove_image=remove_image,
        )
    except svc.FabricationError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("flush failed while updating spare part fabrication %s", fabrication_id)
        return jsonify({"error": _SAVE_FAILED_MSG}), 500

    # On a failed commit, the OLD image (replaced or being removed) is
    # left completely untouched — it is still the correct, live file for
    # the row that the rollback restores. Only a NEW image this request
    # just wrote is cleaned up. See finalize_write().
    if not svc.finalize_write(result):
        return jsonify({"error": _SAVE_FAILED_MSG}), 500

    fabrication = result.fabrication
    part = db.session.get(SparePart, fabrication.spare_part_id)
    return jsonify(_enrich(fabrication, {part.id: part} if part else {}))


@spare_part_fabrications_bp.route("/<int:fabrication_id>", methods=["DELETE"])
@roles_required(ROLE_SUPER_ADMIN)
def delete_fabrication(fabrication_id):
    fabrication = db.session.get(SparePartFabrication, fabrication_id)
    if fabrication is None:
        return jsonify({"error": "not found"}), 404
    try:
        result = svc.delete_fabrication(fabrication, current_user())
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("flush failed while deleting spare part fabrication %s", fabrication_id)
        return jsonify({"error": _DELETE_FAILED_MSG}), 500

    # On a failed commit the record (and therefore its image) must still
    # exist — finalize_write() only deletes the image after a successful
    # commit; nothing is cleaned up here if that commit fails.
    if not svc.finalize_write(result):
        return jsonify({"error": _DELETE_FAILED_MSG}), 500
    return jsonify({"ok": True})


@spare_part_fabrications_bp.route("/<int:fabrication_id>/image", methods=["GET"])
@roles_required(ROLE_SUPER_ADMIN)
def fabrication_image(fabrication_id):
    fabrication = db.session.get(SparePartFabrication, fabrication_id)
    if fabrication is None:
        abort(404)
    path = svc.image_file_path(fabrication)
    if path is None or not os.path.isfile(path):
        abort(404)
    try:
        return send_file(path)
    except OSError:
        abort(404)
