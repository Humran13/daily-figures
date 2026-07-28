import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture
def app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("SUPERADMIN_USERNAME", raising=False)
    monkeypatch.delenv("SUPERADMIN_PASSWORD", raising=False)

    from webapp import create_app
    from webapp.extensions import db as _db

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    with flask_app.app_context():
        _db.create_all()
        yield flask_app
        _db.session.remove()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def make_user(app):
    from webapp.extensions import db as _db
    from webapp.models.user import User

    def _make(username, password, role, active=True):
        user = User(
            username=username,
            password_hash=generate_password_hash(password),
            role=role,
            active=active,
        )
        _db.session.add(user)
        _db.session.commit()
        return user

    return _make


@pytest.fixture
def login_as(client, make_user):
    def _login(username, password, role):
        make_user(username, password, role)
        res = client.post("/api/login", json={"username": username, "password": password})
        assert res.status_code == 200, res.get_json()
        return res.get_json()["user"]

    return _login
