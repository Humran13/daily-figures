"""
Per-user section access grants — mirrors
webapp/services/operator_permissions_service.py's pattern (validate,
diff before/after, audit, flush-not-commit, route owns the commit).

Section access answers WHERE a user may log in; it never touches
`User.role`, which still answers WHAT they may do once inside.
"""
from webapp.extensions import db
from webapp.models.section import (
    DEPARTMENT_STORE, Department, Section, SECTION_FINISHED_GOODS, SECTION_SPARE_PARTS, SECTIONS,
    UserSectionAccess,
)
from webapp.services.audit_service import record_audit


class SectionAccessError(ValueError):
    pass


def ensure_seeded():
    """
    Lazily creates the Store Department + Finished Goods/Spare Parts
    sections if they don't exist yet — same pattern as
    operator_permissions_service.get_permissions() and
    feature_flag_service.get_all_flags(), so this works identically
    whether the tables were populated by the Alembic migration's data seed
    or by a test/dev database created via db.create_all() (which only
    creates schema, never runs a migration's data seed). Flushes but never
    commits — the caller's own commit covers it.
    """
    dept = Department.query.filter_by(code=DEPARTMENT_STORE).first()
    if dept is None:
        dept = Department(code=DEPARTMENT_STORE, name="Store Department", active=True, sort_order=0)
        db.session.add(dept)
        db.session.flush()
    for code, name, sort_order in (
        (SECTION_FINISHED_GOODS, "Finished Goods", 0),
        (SECTION_SPARE_PARTS, "Spare Parts", 1),
    ):
        if Section.query.filter_by(code=code).first() is None:
            db.session.add(Section(department_id=dept.id, code=code, name=name, active=True, sort_order=sort_order))
    db.session.flush()


def user_has_section_access(user, section):
    """
    A user with NO section-access rows at all — every account that
    existed before this feature shipped, or was created directly against
    the User model rather than through the admin-users API (e.g. the
    seeded super-admin, or any test fixture that builds a User() row
    directly) — is treated as finished_goods-only, exactly like it would
    have been the only thing a pre-existing account could ever do. Once a
    user has at least one explicit grant (even just finished_goods, which
    is what the admin "create user" endpoint grants by default), that
    explicit set is authoritative and this fallback no longer applies —
    so revoking finished_goods from such a user actually takes effect.
    """
    from webapp.models.user import ROLE_SUPER_ADMIN
    if user.role == ROLE_SUPER_ADMIN:
        # Global section access: a Super Admin may enter every active
        # section regardless of UserSectionAccess rows — a role check,
        # never a username check, so this applies identically to any
        # current or future super_admin account.
        return True

    has_any_grant = db.session.query(UserSectionAccess.id).filter_by(user_id=user.id).first() is not None
    if not has_any_grant:
        return section.code == SECTION_FINISHED_GOODS
    return (
        db.session.query(UserSectionAccess.id)
        .filter_by(user_id=user.id, section_id=section.id)
        .first()
        is not None
    )


def get_section_codes_for_user(user):
    rows = (
        db.session.query(Section.code)
        .join(UserSectionAccess, UserSectionAccess.section_id == Section.id)
        .filter(UserSectionAccess.user_id == user.id)
        .order_by(Section.sort_order)
        .all()
    )
    return [code for (code,) in rows]


def set_sections_for_user(user, section_codes, actor):
    """
    section_codes: list of section code strings this user should have
    access to afterward (replaces the current set entirely). Rejects
    unknown codes rather than silently ignoring a typo.
    """
    unknown = set(section_codes) - set(SECTIONS)
    if unknown:
        raise SectionAccessError(f"unknown section code(s): {sorted(unknown)}")

    ensure_seeded()
    sections = {s.code: s for s in Section.query.filter(Section.code.in_(section_codes)).all()}
    missing = set(section_codes) - set(sections)
    if missing:
        raise SectionAccessError(f"section code(s) not found: {sorted(missing)}")

    before = sorted(get_section_codes_for_user(user))
    target = set(section_codes)
    current_grants = {g.section_id: g for g in UserSectionAccess.query.filter_by(user_id=user.id).all()}
    current_code_by_section_id = {
        s.id: s.code for s in Section.query.filter(Section.id.in_(current_grants.keys())).all()
    } if current_grants else {}
    current_codes = set(current_code_by_section_id.values())

    for code in target - current_codes:
        db.session.add(UserSectionAccess(user_id=user.id, section_id=sections[code].id))

    for section_id, code in current_code_by_section_id.items():
        if code not in target:
            db.session.delete(current_grants[section_id])

    db.session.flush()
    after = sorted(get_section_codes_for_user(user))

    record_audit(
        actor, "update_section_access", "user", entity_id=user.id,
        before={"sections": before}, after={"sections": after},
    )
