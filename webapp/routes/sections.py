"""
Public, unauthenticated department/section listing for the pre-login
section-selection screen (static/index.html's #sectionScreen) — "all
enabled sections can be visible before login" per the Store
Department/Section Access spec. Carries no user-specific data, so there
is nothing here to protect; the actual authorization check happens
server-side at /api/login (see webapp/auth.py) once a section AND
credentials are both submitted.
"""
from flask import Blueprint, jsonify

from webapp.models.section import Department, Section
from webapp.services import section_access_service

sections_bp = Blueprint("sections", __name__, url_prefix="/api/sections")


@sections_bp.route("", methods=["GET"])
def list_sections():
    section_access_service.ensure_seeded()
    departments = []
    for dept in Department.query.filter_by(active=True).order_by(Department.sort_order, Department.id).all():
        sections = (
            Section.query.filter_by(department_id=dept.id, active=True)
            .order_by(Section.sort_order, Section.id)
            .all()
        )
        departments.append({
            "code": dept.code,
            "name": dept.name,
            "sections": [{"code": s.code, "name": s.name} for s in sections],
        })
    return jsonify({"departments": departments})
