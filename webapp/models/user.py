from datetime import datetime, timezone

from webapp.extensions import db

ROLE_SUPER_ADMIN = "super_admin"
ROLE_MANAGER = "manager"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
ROLES = [ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_OPERATOR, ROLE_VIEWER]


def _utcnow():
    # Naive UTC, deliberately — SQLite has no real timezone-aware storage,
    # so mixing aware and naive datetimes here is how you get
    # "can't compare offset-naive and offset-aware datetimes" crashes.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=ROLE_VIEWER)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)
    last_login_at = db.Column(db.DateTime(), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }
