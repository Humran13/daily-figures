"""
Spare Part Fabrication history — Super-Admin-only financial records of
fabricating a spare part in-house (cost vs. what it was sold to the
factory for). See webapp/models/spare_part_fabrication.py for the data
model and why it is its own history table.

Image handling mirrors webapp/services/branding_service.py's established,
already-reviewed pattern: the actual image BYTES are validated (Pillow
verify + format), never the client's filename or Content-Type, and the
stored filename is always server-generated (uuid4 + the format detected
from the bytes) — this is what prevents path traversal and content-type
spoofing, not sanitizing a client-supplied name. Images live under
DB_PATH's own persistent directory (uploads/fabrications), the same
already-mounted volume branding logos use, so no new Docker volume is
needed and nothing is ever written under static/ or the Git-tracked tree.
"""
import io
import os
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from PIL import Image, UnidentifiedImageError
from sqlalchemy import or_

from webapp.extensions import db
from webapp.models.spare_part import SparePart
from webapp.models.spare_part_fabrication import SparePartFabrication
from webapp.services.audit_service import record_audit
from webapp.services.branding_service import FORMAT_TO_EXT

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB


class FabricationError(ValueError):
    pass


def _uploads_dir():
    from flask import current_app
    base = os.path.dirname(current_app.config["DB_PATH"])
    path = os.path.join(base, "uploads", "fabrications")
    os.makedirs(path, exist_ok=True)
    return path


def image_file_path(fabrication):
    """Resolves a fabrication's stored image to an absolute path, or None
    if it has no image. os.path.basename defends against image_path ever
    being anything other than the plain server-generated filename it is
    always written as — a stored value can never walk outside
    _uploads_dir() via '..' or an absolute path."""
    if not fabrication.image_path:
        return None
    return os.path.join(_uploads_dir(), os.path.basename(fabrication.image_path))


def _delete_image_file(filename):
    if not filename:
        return
    path = os.path.join(_uploads_dir(), os.path.basename(filename))
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass  # never fail the request over cleanup of an old/orphaned file


def _validate_whole_ugx(value, field_name):
    if value is None or value == "":
        raise FabricationError(f"{field_name} is required")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise FabricationError(f"{field_name} must be a whole number") from None
    if decimal_value != decimal_value.to_integral_value():
        raise FabricationError(f"{field_name} must be a whole UGX amount (no cents)")
    if decimal_value < 0:
        raise FabricationError(f"{field_name} cannot be negative")
    return int(decimal_value)


def _validate_date(value):
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        raise FabricationError("fabrication_date must be a valid YYYY-MM-DD date") from None
    return value


def _validate_and_read_image(file_storage):
    """Returns (bytes, extension), or (None, None) when no file was
    actually chosen (an empty multipart field) — distinct from an
    invalid/corrupt upload, which raises."""
    if file_storage is None or not file_storage.filename:
        return None, None
    file_storage.stream.seek(0)
    data = file_storage.stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) == 0:
        raise FabricationError("Uploaded image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise FabricationError(f"Image exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)}MB limit")
    try:
        Image.open(io.BytesIO(data)).verify()
        fmt = Image.open(io.BytesIO(data)).format  # verify() invalidates the first handle for further use
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise FabricationError("Unsupported or unreadable image file") from e
    ext = FORMAT_TO_EXT.get(fmt)
    if ext is None:
        raise FabricationError("Unsupported image format — JPEG, PNG, and WebP are supported")
    return data, ext


def _store_image(file_storage):
    """Validates and writes a new image file, returning its server-
    generated filename, or None if no file was supplied."""
    data, ext = _validate_and_read_image(file_storage)
    if data is None:
        return None
    filename = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(_uploads_dir(), filename), "wb") as f:
        f.write(data)
    return filename


def _audit_snapshot(fabrication):
    """Non-secret before/after for the audit log: financial amounts are
    fine (Super-Admin-only, matches existing price_change convention),
    but never the filesystem path — has_image is the boolean equivalent
    callers/the UI ever see."""
    return {
        "spare_part_id": fabrication.spare_part_id,
        "fabrication_date": fabrication.fabrication_date,
        "fabrication_cost_ugx": fabrication.fabrication_cost_ugx,
        "sold_to_factory_ugx": fabrication.sold_to_factory_ugx,
        "profit_loss_ugx": fabrication.profit_loss_ugx,
        "has_image": fabrication.image_path is not None,
        "notes": fabrication.notes,
    }


def create_fabrication(*, spare_part_id, fabrication_date, fabrication_cost_ugx,
                        sold_to_factory_ugx, user, notes=None, image_file_storage=None):
    spare_part = db.session.get(SparePart, spare_part_id)
    if spare_part is None:
        raise FabricationError("unknown spare_part_id")
    cost = _validate_whole_ugx(fabrication_cost_ugx, "Fabrication Cost")
    sold = _validate_whole_ugx(sold_to_factory_ugx, "Sold to Factory")
    fabrication_date = _validate_date(fabrication_date)

    image_filename = _store_image(image_file_storage)
    fabrication = SparePartFabrication(
        spare_part_id=spare_part_id, fabrication_date=fabrication_date,
        fabrication_cost_ugx=cost, sold_to_factory_ugx=sold,
        image_path=image_filename, notes=(notes or "").strip() or None,
        created_by_user_id=user.id,
    )
    db.session.add(fabrication)
    try:
        db.session.flush()
    except Exception:
        # Never leave an orphan file behind a DB row that never committed.
        _delete_image_file(image_filename)
        raise
    record_audit(user, "fabrication_create", "spare_part_fabrication",
                 entity_id=fabrication.id, after=_audit_snapshot(fabrication))
    return fabrication


def update_fabrication(fabrication, changes, user, *, image_file_storage=None, remove_image=False):
    before = _audit_snapshot(fabrication)
    old_image_path = fabrication.image_path

    if "spare_part_id" in changes:
        spare_part = db.session.get(SparePart, changes["spare_part_id"])
        if spare_part is None:
            raise FabricationError("unknown spare_part_id")
        fabrication.spare_part_id = spare_part.id
    if "fabrication_date" in changes:
        fabrication.fabrication_date = _validate_date(changes["fabrication_date"])
    if "fabrication_cost_ugx" in changes:
        fabrication.fabrication_cost_ugx = _validate_whole_ugx(changes["fabrication_cost_ugx"], "Fabrication Cost")
    if "sold_to_factory_ugx" in changes:
        fabrication.sold_to_factory_ugx = _validate_whole_ugx(changes["sold_to_factory_ugx"], "Sold to Factory")
    if "notes" in changes:
        fabrication.notes = (changes["notes"] or "").strip() or None

    new_image_filename = None
    if image_file_storage is not None and image_file_storage.filename:
        new_image_filename = _store_image(image_file_storage)
        fabrication.image_path = new_image_filename
    elif remove_image:
        fabrication.image_path = None

    try:
        db.session.flush()
    except Exception:
        if new_image_filename:
            _delete_image_file(new_image_filename)
        raise

    record_audit(user, "fabrication_update", "spare_part_fabrication",
                 entity_id=fabrication.id, before=before, after=_audit_snapshot(fabrication))

    # Only remove the OLD file once the replacement has safely committed.
    if old_image_path and old_image_path != fabrication.image_path:
        _delete_image_file(old_image_path)
    return fabrication


def delete_fabrication(fabrication, user):
    before = _audit_snapshot(fabrication)
    image_path = fabrication.image_path
    fabrication_id = fabrication.id
    db.session.delete(fabrication)
    db.session.flush()
    record_audit(user, "fabrication_delete", "spare_part_fabrication", entity_id=fabrication_id, before=before)
    _delete_image_file(image_path)


def _escape_like(token):
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_fabrications(*, search=None, date_from=None, date_to=None):
    query = SparePartFabrication.query.join(SparePart, SparePart.id == SparePartFabrication.spare_part_id)
    if date_from:
        query = query.filter(SparePartFabrication.fabrication_date >= date_from)
    if date_to:
        query = query.filter(SparePartFabrication.fabrication_date <= date_to)
    search = (search or "").strip()
    if search:
        pattern = f"%{_escape_like(search)}%"
        query = query.filter(or_(
            SparePart.name.ilike(pattern, escape="\\"),
            SparePart.specifications.ilike(pattern, escape="\\"),
        ))
    return query.order_by(SparePartFabrication.fabrication_date.desc(), SparePartFabrication.id.desc()).all()
