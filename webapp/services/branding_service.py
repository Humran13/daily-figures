"""
Company Settings / white-label branding. Kept deliberately separate from
operational business data — this module never touches dispatches,
customers, or daily figures.

Logo files live under DB_PATH's own directory (the persistent Docker
volume already mounted for the database — see webapp/__init__.py), so no
new volume or environment variable is needed to survive a container
restart. Filenames are always server-generated (uuid4 + a format detected
from the actual image bytes, never the client's original filename or
extension) — this is what prevents path traversal and content-type
spoofing, not input sanitization of a client-supplied name.
"""
import io
import os
import re
import uuid

from PIL import Image, UnidentifiedImageError

from webapp.extensions import db
from webapp.models.company_settings import CompanySettings
from webapp.services.audit_service import record_audit

MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2MB
FORMAT_TO_EXT = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}

TEXT_FIELD_MAX_LENGTHS = {
    "display_name": 160,
    "legal_name": 160,
    "address": 500,
    "phone": 40,
    "email": 160,
    "website": 200,
    "currency_code": 10,
    "tax_registration_number": 80,
    "report_footer_text": 500,
    "primary_contact_name": 120,
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_WEBSITE_RE = re.compile(r"^https?://[^\s]+\.[^\s]+$")


class BrandingError(ValueError):
    pass


def _uploads_dir():
    from flask import current_app
    base = os.path.dirname(current_app.config["DB_PATH"])
    path = os.path.join(base, "uploads", "branding")
    os.makedirs(path, exist_ok=True)
    return path


def logo_file_path(settings=None):
    settings = settings or get_settings()
    if not settings.logo_path:
        return None
    return os.path.join(_uploads_dir(), settings.logo_path)


def export_kwargs():
    """
    One-line spread for every export route: **branding_service.export_kwargs()
    into build_export(fmt, ...). Keeps export_service.py itself free of any
    DB/app-context dependency (see its module docstring) while still giving
    every PDF/Excel export the current company branding without duplicating
    the get_settings()/logo_file_path() pair at each of the 4 call sites.
    """
    settings = get_settings()
    return {"company_settings": settings, "logo_path": logo_file_path(settings)}


def get_settings():
    settings = db.session.get(CompanySettings, 1)
    if settings is None:
        # Only reachable on a test DB created via db.create_all() rather
        # than the seeded migration — same lazy-default pattern used
        # elsewhere in this codebase (e.g. operator_permissions_service).
        settings = CompanySettings(id=1, display_name="Daily Figures")
        db.session.add(settings)
        db.session.flush()
    return settings


def update_settings(changes, user):
    settings = get_settings()
    unknown = set(changes) - set(TEXT_FIELD_MAX_LENGTHS)
    if unknown:
        raise BrandingError(f"unknown field(s): {sorted(unknown)}")

    normalized = {}
    for field, value in changes.items():
        if isinstance(value, str):
            value = value.strip()
            if not value and field != "display_name":
                value = None
        normalized[field] = value

    if "display_name" in normalized and not normalized["display_name"]:
        raise BrandingError("Company display name is required")
    if normalized.get("email") and not _EMAIL_RE.match(normalized["email"]):
        raise BrandingError("Invalid email address")
    if normalized.get("website") and not _WEBSITE_RE.match(normalized["website"]):
        raise BrandingError("Invalid website URL — must start with http:// or https://")
    for field, value in normalized.items():
        max_len = TEXT_FIELD_MAX_LENGTHS[field]
        if value and len(value) > max_len:
            raise BrandingError(f"{field} must be at most {max_len} characters")

    before = settings.to_dict()
    for field, value in normalized.items():
        setattr(settings, field, value)
    settings.updated_by = user.id
    db.session.flush()
    after = settings.to_dict()

    record_audit(user, "update", "company_settings", entity_id=1, before=before, after=after)
    return settings


def _validate_and_read_logo(file_storage):
    if file_storage is None or not file_storage.filename:
        raise BrandingError("No file uploaded")
    file_storage.stream.seek(0)
    data = file_storage.stream.read(MAX_LOGO_BYTES + 1)
    if len(data) == 0:
        raise BrandingError("Uploaded file is empty")
    if len(data) > MAX_LOGO_BYTES:
        raise BrandingError(f"Logo file exceeds the {MAX_LOGO_BYTES // (1024 * 1024)}MB limit")

    # The actual image bytes are what's trusted here — never the client's
    # filename or Content-Type header, both of which are trivially spoofed.
    try:
        Image.open(io.BytesIO(data)).verify()
        fmt = Image.open(io.BytesIO(data)).format  # verify() invalidates the first handle for further use
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise BrandingError("Unsupported or unreadable image file") from e

    ext = FORMAT_TO_EXT.get(fmt)
    if ext is None:
        raise BrandingError("Unsupported image format — PNG, JPEG, and WebP are supported")
    return data, ext


def upload_logo(file_storage, user):
    data, ext = _validate_and_read_logo(file_storage)
    settings = get_settings()
    before = settings.to_dict()
    old_relative_path = settings.logo_path

    filename = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(_uploads_dir(), filename), "wb") as f:
        f.write(data)

    settings.logo_path = filename
    settings.updated_by = user.id
    db.session.flush()
    after = settings.to_dict()
    record_audit(user, "logo_replace" if old_relative_path else "logo_upload",
                 "company_settings", entity_id=1, before=before, after=after)

    if old_relative_path:
        _delete_logo_file(old_relative_path)
    return settings


def remove_logo(user):
    settings = get_settings()
    if not settings.logo_path:
        raise BrandingError("No logo is currently set")
    before = settings.to_dict()
    old_relative_path = settings.logo_path
    settings.logo_path = None
    settings.updated_by = user.id
    db.session.flush()
    after = settings.to_dict()
    record_audit(user, "logo_remove", "company_settings", entity_id=1, before=before, after=after)
    _delete_logo_file(old_relative_path)
    return settings


def _delete_logo_file(relative_path):
    full_path = os.path.join(_uploads_dir(), relative_path)
    try:
        if os.path.exists(full_path):
            os.remove(full_path)
    except OSError:
        pass  # never fail the request over cleanup of the previous file
