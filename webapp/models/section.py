from datetime import datetime, timezone

from webapp.extensions import db

DEPARTMENT_STORE = "store"

SECTION_FINISHED_GOODS = "finished_goods"
SECTION_SPARE_PARTS = "spare_parts"
SECTIONS = [SECTION_FINISHED_GOODS, SECTION_SPARE_PARTS]


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Department(db.Model):
    __tablename__ = "departments"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    def to_dict(self):
        return {"id": self.id, "code": self.code, "name": self.name, "active": self.active}


class Section(db.Model):
    __tablename__ = "sections"

    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=False)
    code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    def to_dict(self):
        return {
            "id": self.id,
            "department_id": self.department_id,
            "code": self.code,
            "name": self.name,
            "active": self.active,
        }


class UserSectionAccess(db.Model):
    """
    Which sections a user may log into. Separate from `User.role` on
    purpose (section access says WHERE a user may enter, role says WHAT
    they may do once inside — see BUILD SECTION/DEPARTMENT ACCESS
    ARCHITECTURE spec). A user's role stays global across every section
    they have access to; this table never affects role checks.
    """
    __tablename__ = "user_section_access"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey("sections.id"), nullable=False)
    created_at = db.Column(db.DateTime(), nullable=False, default=_utcnow)

    __table_args__ = (db.UniqueConstraint("user_id", "section_id", name="uq_user_section_access_user_section"),)
