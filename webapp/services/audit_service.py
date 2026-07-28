import json

from flask import has_request_context, request

from webapp.extensions import db
from webapp.models.audit_log import AuditLog


def _client_ip():
    # record_audit() is also called from startup/CLI code paths (e.g.
    # seed_super_admin) that run outside any HTTP request.
    if not has_request_context():
        return None
    return request.remote_addr


def record_audit(user, action, entity_type, entity_id=None, before=None, after=None):
    """
    Write one audit row. Never raises past a failed write's own transaction —
    callers should not lose the primary action because logging hiccuped, but
    in practice this runs in the same commit as the action it describes.
    """
    entry = AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        before_json=json.dumps(before, default=str) if before is not None else None,
        after_json=json.dumps(after, default=str) if after is not None else None,
        ip_address=_client_ip(),
    )
    db.session.add(entry)
    return entry
