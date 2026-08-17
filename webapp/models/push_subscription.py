"""
Web Push subscription storage — one row per browser/device a user has
opted into notifications on (a user may have several: phone + desktop).
No push payload content is ever stored here, only the delivery endpoint
and the encryption keys the Push API itself requires — see
webapp/services/push_service.py for how these are used, and its own
docstring for why actual delivery gracefully no-ops without VAPID keys
configured in the deployment environment.
"""
from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PushSubscription(db.Model):
    __tablename__ = "push_subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    # The PushSubscription.endpoint URL the browser's push service issued —
    # unique per browser/device registration; re-subscribing the same
    # device updates the existing row rather than creating a duplicate.
    endpoint = db.Column(db.String(500), nullable=False, unique=True, index=True)
    p256dh = db.Column(db.String(255), nullable=False)
    auth = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)

    user = db.relationship("User")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "endpoint": self.endpoint,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
