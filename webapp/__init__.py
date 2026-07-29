"""
Application factory. Wires together the legacy Daily Figures entries API
(unchanged, raw-sqlite3, preserved for zero data risk) with the new
SQLAlchemy-backed subsystem: users/auth, products, packaging rules,
customers, and the audit log.
"""
import os

from flask import Flask, jsonify
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
