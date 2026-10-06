"""
Application factory. Wires together the legacy Daily Figures entries API
(unchanged, raw-sqlite3, preserved for zero data risk) with the new
SQLAlchemy-backed subsystem: users/auth, products, packaging rules,
customers, and the audit log.
"""
import os

from flask import Flask, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

from webapp.extensions import db, migrate


def create_app():
    app = Flask(
        __name__,
        static_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), "static"),
        static_url_path="",
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        raise RuntimeError(
            "SECRET_KEY environment variable is required and was not set. "
            "Sessions cannot be signed securely without it — refusing to start. "
            "See .env.example for how to set it (docker-entrypoint.sh also checks "
            "this before the container even reaches this point)."
        )
    app.secret_key = secret_key

    db_path = os.environ.get("DB_PATH", "/app/data/production.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"timeout": 15}}
    app.config["DB_PATH"] = db_path

    db.init_app(app)
    migrate.init_app(app, db, directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations"))

    # Legacy entries table/API — untouched raw-sqlite3 logic, still lives on
    # DB_PATH. Must run before the new models so the table exists either way.
    from webapp.legacy_entries import init_legacy_db, legacy_bp
    init_legacy_db(db_path)
    app.register_blueprint(legacy_bp)

    from webapp.auth import auth_bp, seed_super_admin
    from webapp.routes.admin_products import admin_products_bp
    from webapp.routes.admin_customers import admin_customers_bp
    from webapp.routes.admin_users import admin_users_bp
    from webapp.routes.admin_sales_categories import admin_sales_categories_bp
    from webapp.routes.admin_recipient_import import admin_recipient_import_bp
    from webapp.routes.dispatches import dispatches_bp
    from webapp.routes.daily_figures import daily_figures_bp
    from webapp.routes.admin_legacy import admin_legacy_bp
    from webapp.routes.reports import reports_bp
    from webapp.routes.dashboard import dashboard_bp
    from webapp.routes.admin_operator_permissions import admin_operator_permissions_bp
    from webapp.routes.pages import pages_bp
    from webapp.routes.branding import branding_bp
    from webapp.routes.admin_company_settings import admin_company_settings_bp
    from webapp.routes.feature_flags import feature_flags_bp
    from webapp.routes.returns import returns_bp
    from webapp.routes.production import production_bp
    from webapp.routes.pwa import pwa_bp
    from webapp.routes.daily_entry_status import daily_entry_status_bp
    from webapp.routes.daily_reset import daily_reset_bp
    from webapp.routes.daily_review import daily_review_bp
    from webapp.routes.ledger_cutover import ledger_cutover_bp
    from webapp.routes.correction_requests import correction_requests_bp
    from webapp.routes.push import push_bp
    from webapp.routes.sections import sections_bp
    from webapp.routes.spare_parts import spare_parts_bp
    from webapp.routes.spare_part_machines import spare_part_machines_bp
    from webapp.routes.spare_part_suppliers import spare_part_suppliers_bp
    from webapp.routes.spare_part_movements import spare_part_movements_bp
    from webapp.routes.spare_part_categories import spare_part_categories_bp
    from webapp.routes.spare_part_departments import spare_part_departments_bp
    from webapp.routes.admin_spare_pricing import admin_spare_pricing_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_products_bp)
    app.register_blueprint(admin_customers_bp)
    app.register_blueprint(admin_users_bp)
    app.register_blueprint(admin_sales_categories_bp)
    app.register_blueprint(admin_recipient_import_bp)
    app.register_blueprint(dispatches_bp)
    app.register_blueprint(daily_figures_bp)
    app.register_blueprint(admin_legacy_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_operator_permissions_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(branding_bp)
    app.register_blueprint(admin_company_settings_bp)
    app.register_blueprint(feature_flags_bp)
    app.register_blueprint(returns_bp)
    app.register_blueprint(production_bp)
    app.register_blueprint(daily_entry_status_bp)
    app.register_blueprint(daily_reset_bp)
    app.register_blueprint(daily_review_bp)
    app.register_blueprint(ledger_cutover_bp)
    app.register_blueprint(correction_requests_bp)
    app.register_blueprint(push_bp)
    app.register_blueprint(pwa_bp)
    app.register_blueprint(sections_bp)
    app.register_blueprint(spare_parts_bp)
    app.register_blueprint(spare_part_machines_bp)
    app.register_blueprint(spare_part_suppliers_bp)
    app.register_blueprint(spare_part_movements_bp)
    app.register_blueprint(spare_part_categories_bp)
    app.register_blueprint(spare_part_departments_bp)
    app.register_blueprint(admin_spare_pricing_bp)

    # Store Department / Section Access: these blueprints are each either
    # Finished-Goods or Spare-Parts operational data. A single app-level
    # before_request hook, keyed off request.blueprint, is the smallest
    # centralized way to section-gate every route they contain — including
    # any added later — without decorating dozens of individual view
    # functions. (Blueprint.before_request() isn't used here because
    # Blueprint objects are module-level singletons reused across every
    # create_app() call, e.g. once per test; Flask forbids adding a setup
    # hook to a blueprint that's already been registered once elsewhere, so
    # a per-app hook is the only option that's safe to call from inside
    # create_app() every time.) Admin/auth/infra blueprints (user
    # management, feature flags, branding, company settings, push,
    # PWA manifest) are deliberately left ungated: they are global, not
    # tied to any one section, per the architecture spec.
    from webapp.auth import enforce_section
    from webapp.models.section import SECTION_FINISHED_GOODS, SECTION_SPARE_PARTS
    _SECTION_BY_BLUEPRINT = {}
    for fg_bp in (
        legacy_bp, dispatches_bp, daily_figures_bp, reports_bp, dashboard_bp,
        returns_bp, production_bp, daily_entry_status_bp, daily_reset_bp,
        daily_review_bp, ledger_cutover_bp, correction_requests_bp,
    ):
        _SECTION_BY_BLUEPRINT[fg_bp.name] = SECTION_FINISHED_GOODS
    for sp_bp in (
        spare_parts_bp, spare_part_machines_bp, spare_part_suppliers_bp, spare_part_movements_bp,
        spare_part_categories_bp, spare_part_departments_bp,
    ):
        _SECTION_BY_BLUEPRINT[sp_bp.name] = SECTION_SPARE_PARTS

    @app.before_request
    def _enforce_section_by_blueprint():
        required = _SECTION_BY_BLUEPRINT.get(request.blueprint)
        if required:
            return enforce_section(required)
        return None

    from webapp.cli import register_cli
    register_cli(app)

    @app.route("/api/health")
    def health():
        # Unauthenticated on purpose — this is for Docker/load-balancer
        # health checks, not application data. Confirms the process is up
        # and the database file is actually reachable.
        try:
            db.session.execute(db.text("SELECT 1"))
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"status": "error", "detail": str(e)}), 503

    with app.app_context():
        try:
            seed_super_admin()
        except Exception:
            # Tables don't exist yet (e.g. `flask db init`/`migrate` running
            # before the first `flask db upgrade`, or a fresh empty volume).
            # Real startup always runs migrations first — see DEPLOY.md.
            db.session.rollback()

    return app
