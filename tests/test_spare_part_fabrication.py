"""
Spare Part Fabrication — Super-Admin-only financial history (cost vs.
amount sold to the factory). See
webapp/services/spare_part_fabrication_service.py and
webapp/routes/spare_part_fabrications.py.
"""
import io
import os

import pytest
from PIL import Image
from sqlalchemy.exc import SQLAlchemyError

from webapp.extensions import db
from webapp.models.spare_part_fabrication import SparePartFabrication
from webapp.services import spare_part_fabrication_service as svc

from tests.test_spare_parts import _create_spare_part, _login_spare_parts


def _png_bytes(color=(255, 0, 0)):
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


def _create(client, spare_part_id, **overrides):
    data = {
        "spare_part_id": str(spare_part_id),
        "fabrication_date": "2026-02-05",
        "fabrication_cost_ugx": "180000",
        "sold_to_factory_ugx": "230000",
    }
    data.update(overrides)
    return client.post("/api/spare-parts/fabrications", data=data, content_type="multipart/form-data")


@pytest.fixture
def super_admin_client(client, make_user):
    _login_spare_parts(client, make_user, "fab_super1", "super_admin")
    part = _create_spare_part(client, name="Custom Shaft", specifications="1200mm")
    return client, part


# ---------- CRUD ----------

def test_super_admin_can_create_fabrication_without_image(super_admin_client):
    client, part = super_admin_client
    res = _create(client, part["id"])
    assert res.status_code == 201, res.get_json()
    body = res.get_json()
    assert body["fabrication_cost_ugx"] == 180000
    assert body["sold_to_factory_ugx"] == 230000
    assert body["profit_loss_ugx"] == 50000
    assert body["has_image"] is False
    assert body["display_name"] == "Custom Shaft [1200mm]"


def test_super_admin_can_create_with_valid_jpeg(super_admin_client):
    client, part = super_admin_client
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (0, 255, 0)).save(buf, format="JPEG")
    res = _create(client, part["id"], image=(io.BytesIO(buf.getvalue()), "part.jpg"))
    assert res.status_code == 201, res.get_json()
    assert res.get_json()["has_image"] is True


def test_png_accepted(super_admin_client):
    client, part = super_admin_client
    res = _create(client, part["id"], image=(io.BytesIO(_png_bytes()), "part.png"))
    assert res.status_code == 201
    assert res.get_json()["has_image"] is True


def test_webp_accepted(super_admin_client):
    client, part = super_admin_client
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (0, 0, 255)).save(buf, format="WEBP")
    res = _create(client, part["id"], image=(io.BytesIO(buf.getvalue()), "part.webp"))
    assert res.status_code == 201
    assert res.get_json()["has_image"] is True


def test_invalid_non_image_file_rejected(super_admin_client):
    client, part = super_admin_client
    res = _create(client, part["id"], image=(io.BytesIO(b"not an image, just text"), "fake.png"))
    assert res.status_code == 400
    assert "Unsupported" in res.get_json()["error"] or "unreadable" in res.get_json()["error"]


def test_oversized_file_rejected(app, super_admin_client):
    client, part = super_admin_client
    from webapp.services import spare_part_fabrication_service as svc
    big = _png_bytes() + b"\x00" * (svc.MAX_IMAGE_BYTES + 10)
    res = _create(client, part["id"], image=(io.BytesIO(big), "big.png"))
    assert res.status_code == 400
    assert "exceeds" in res.get_json()["error"]


def test_stored_filename_is_server_generated_and_image_persists(app, super_admin_client):
    client, part = super_admin_client
    res = _create(client, part["id"], image=(io.BytesIO(_png_bytes()), "../../evil.png"))
    assert res.status_code == 201
    fid = res.get_json()["id"]
    with app.app_context():
        fab = db.session.get(SparePartFabrication, fid)
        assert fab.image_path is not None
        assert ".." not in fab.image_path
        assert "/" not in fab.image_path and "\\" not in fab.image_path
        assert fab.image_path != "../../evil.png"

    img_res = client.get(f"/api/spare-parts/fabrications/{fid}/image")
    assert img_res.status_code == 200
    assert img_res.data  # survives reading back from the persistent upload path


def test_multiple_fabrication_records_may_exist_for_one_spare_part(super_admin_client):
    client, part = super_admin_client
    res1 = _create(client, part["id"], fabrication_date="2026-02-05")
    res2 = _create(client, part["id"], fabrication_date="2026-06-10", fabrication_cost_ugx="195000", sold_to_factory_ugx="250000")
    assert res1.status_code == 201 and res2.status_code == 201
    rows = client.get("/api/spare-parts/fabrications").get_json()
    assert len([r for r in rows if r["spare_part_id"] == part["id"]]) == 2


def test_history_ordering_newest_fabrication_date_first(super_admin_client):
    client, part = super_admin_client
    _create(client, part["id"], fabrication_date="2026-01-01")
    _create(client, part["id"], fabrication_date="2026-06-10")
    rows = client.get("/api/spare-parts/fabrications").get_json()
    dates = [r["fabrication_date"] for r in rows if r["spare_part_id"] == part["id"]]
    assert dates == sorted(dates, reverse=True)


def test_edit_recomputes_profit_correctly(super_admin_client):
    client, part = super_admin_client
    fid = _create(client, part["id"]).get_json()["id"]
    res = client.patch(f"/api/spare-parts/fabrications/{fid}",
                        data={"sold_to_factory_ugx": "150000"}, content_type="multipart/form-data")
    assert res.status_code == 200
    assert res.get_json()["profit_loss_ugx"] == 150000 - 180000
    assert res.get_json()["profit_loss_ugx"] < 0


def test_delete_removes_record_and_image_cleanup_is_safe(app, super_admin_client):
    client, part = super_admin_client
    fid = _create(client, part["id"], image=(io.BytesIO(_png_bytes()), "p.png")).get_json()["id"]
    res = client.delete(f"/api/spare-parts/fabrications/{fid}")
    assert res.status_code == 200
    with app.app_context():
        assert db.session.get(SparePartFabrication, fid) is None
    assert client.get(f"/api/spare-parts/fabrications/{fid}/image").status_code == 404


# ---------- money validation ----------

def test_cost_accepts_whole_nonnegative_ugx(super_admin_client):
    client, part = super_admin_client
    res = _create(client, part["id"], fabrication_cost_ugx="0", sold_to_factory_ugx="0")
    assert res.status_code == 201
    assert res.get_json()["fabrication_cost_ugx"] == 0


def test_fractional_ugx_rejected(super_admin_client):
    client, part = super_admin_client
    res = _create(client, part["id"], fabrication_cost_ugx="180000.50")
    assert res.status_code == 400


def test_negative_cost_rejected(super_admin_client):
    client, part = super_admin_client
    res = _create(client, part["id"], fabrication_cost_ugx="-100")
    assert res.status_code == 400


def test_fractional_sold_amount_rejected(super_admin_client):
    client, part = super_admin_client
    res = _create(client, part["id"], sold_to_factory_ugx="230000.99")
    assert res.status_code == 400


def test_negative_sold_amount_rejected(super_admin_client):
    client, part = super_admin_client
    res = _create(client, part["id"], sold_to_factory_ugx="-1")
    assert res.status_code == 400


def test_loss_is_calculated_correctly_and_not_rejected(super_admin_client):
    client, part = super_admin_client
    res = _create(client, part["id"], fabrication_cost_ugx="250000", sold_to_factory_ugx="220000")
    assert res.status_code == 201
    assert res.get_json()["profit_loss_ugx"] == -30000


def test_profit_not_trusted_from_client_payload(super_admin_client):
    client, part = super_admin_client
    res = client.post("/api/spare-parts/fabrications", data={
        "spare_part_id": str(part["id"]), "fabrication_date": "2026-02-05",
        "fabrication_cost_ugx": "100000", "sold_to_factory_ugx": "150000",
        "profit_loss_ugx": "999999999",  # must be ignored entirely
    }, content_type="multipart/form-data")
    assert res.status_code == 201
    assert res.get_json()["profit_loss_ugx"] == 50000


# ---------- financial isolation / authorization ----------

@pytest.mark.parametrize("role", ["manager", "accountant", "operator", "viewer"])
def test_non_super_admin_cannot_list_or_create_or_image(app, client, make_user, role):
    other = app.test_client()
    sa_user = _login_spare_parts(other, make_user, f"fab_sa_{role}", "super_admin")
    part = _create_spare_part(other, name="Isolated Part")
    fid = _create(other, part["id"]).get_json()["id"]

    _login_spare_parts(client, make_user, f"fab_{role}", role)
    assert client.get("/api/spare-parts/fabrications").status_code == 403
    assert _create(client, part["id"]).status_code == 403
    assert client.patch(f"/api/spare-parts/fabrications/{fid}", data={"notes": "x"},
                         content_type="multipart/form-data").status_code == 403
    assert client.delete(f"/api/spare-parts/fabrications/{fid}").status_code == 403
    assert client.get(f"/api/spare-parts/fabrications/{fid}/image").status_code == 403


def test_super_admin_can_perform_all_fabrication_functions(super_admin_client):
    client, part = super_admin_client
    fid = _create(client, part["id"]).get_json()["id"]
    assert client.get("/api/spare-parts/fabrications").status_code == 200
    assert client.patch(f"/api/spare-parts/fabrications/{fid}", data={"notes": "ok"},
                         content_type="multipart/form-data").status_code == 200
    assert client.delete(f"/api/spare-parts/fabrications/{fid}").status_code == 200


def test_ordinary_spare_parts_api_reveals_zero_fabrication_fields(super_admin_client):
    client, part = super_admin_client
    _create(client, part["id"])
    rows = client.get("/api/spare-parts?include_inactive=1").get_json()
    row = next(r for r in rows if r["id"] == part["id"])
    for forbidden in ("fabrication_cost_ugx", "sold_to_factory_ugx", "profit_loss_ugx"):
        assert forbidden not in row


def test_existing_spare_parts_pricing_security_remains_intact(app, client, make_user):
    _login_spare_parts(client, make_user, "fab_pricing_mgr", "manager")
    part = _create_spare_part(client)
    rows = client.get("/api/spare-parts?include_inactive=1").get_json()
    row = next(r for r in rows if r["id"] == part["id"])
    assert "buying_price" not in row  # manager still never sees pricing


# ---------- transaction-safe file lifecycle (commit-failure simulation) ----------
# These prove the ACTUAL ordering (write/stage -> commit -> cleanup) by making
# db.session.commit() itself fail, not merely a flush-time validation error —
# a real defect here would orphan/delete the wrong file on a genuine DB hiccup.

class _CommitBreaker:
    """Replaces db.session.commit for exactly ONE call with one that raises
    SQLAlchemyError, then (if ever called again) behaves normally again —
    so only the single request under test is affected, never the test's
    other setup/teardown commits."""
    def __init__(self, original):
        self._original = original
        self.triggered = False

    def __call__(self, *args, **kwargs):
        if not self.triggered:
            self.triggered = True
            raise SQLAlchemyError("simulated commit failure")
        return self._original(*args, **kwargs)


def _break_next_commit(monkeypatch):
    breaker = _CommitBreaker(db.session.commit)
    monkeypatch.setattr(db.session, "commit", breaker)
    return breaker


def _uploaded_files(app):
    with app.app_context():
        from flask import current_app
        path = os.path.join(os.path.dirname(current_app.config["DB_PATH"]), "uploads", "fabrications")
        return set(os.listdir(path)) if os.path.isdir(path) else set()


def test_create_with_image_commit_failure_leaves_no_record_or_orphan_image(app, super_admin_client, monkeypatch):
    client, part = super_admin_client
    before_files = _uploaded_files(app)
    _break_next_commit(monkeypatch)

    res = _create(client, part["id"], image=(io.BytesIO(_png_bytes()), "p.png"))
    assert res.status_code == 500
    assert "error" in res.get_json()
    for leak in ("sql", "Traceback", ".db", "uploads"):
        assert leak.lower() not in res.get_json()["error"].lower()

    with app.app_context():
        assert SparePartFabrication.query.filter_by(spare_part_id=part["id"]).count() == 0
    assert _uploaded_files(app) == before_files  # no orphan image left behind


def test_update_replace_image_commit_failure_keeps_original_image_and_reference(app, super_admin_client, monkeypatch):
    client, part = super_admin_client
    fid = _create(client, part["id"], image=(io.BytesIO(_png_bytes((255, 0, 0))), "old.png")).get_json()["id"]
    with app.app_context():
        original_image_path = db.session.get(SparePartFabrication, fid).image_path
    files_before = _uploaded_files(app)
    assert original_image_path in files_before

    _break_next_commit(monkeypatch)
    res = client.patch(f"/api/spare-parts/fabrications/{fid}",
                        data={"image": (io.BytesIO(_png_bytes((0, 255, 0))), "new.png")},
                        content_type="multipart/form-data")
    assert res.status_code == 500

    with app.app_context():
        fab = db.session.get(SparePartFabrication, fid)
        assert fab.image_path == original_image_path  # DB still references the ORIGINAL image

    files_after = _uploaded_files(app)
    assert original_image_path in files_after  # original image still exists
    assert files_after == files_before  # the staged replacement was removed, no net new file

    img_res = client.get(f"/api/spare-parts/fabrications/{fid}/image")
    assert img_res.status_code == 200  # original image still servable


def test_update_remove_image_commit_failure_keeps_original_image_and_reference(app, super_admin_client, monkeypatch):
    client, part = super_admin_client
    fid = _create(client, part["id"], image=(io.BytesIO(_png_bytes()), "p.png")).get_json()["id"]
    with app.app_context():
        original_image_path = db.session.get(SparePartFabrication, fid).image_path
    files_before = _uploaded_files(app)

    _break_next_commit(monkeypatch)
    res = client.patch(f"/api/spare-parts/fabrications/{fid}",
                        data={"remove_image": "1"}, content_type="multipart/form-data")
    assert res.status_code == 500

    with app.app_context():
        fab = db.session.get(SparePartFabrication, fid)
        assert fab.image_path == original_image_path  # relationship to the image is unchanged
    assert _uploaded_files(app) == files_before  # original image still exists, untouched
    assert client.get(f"/api/spare-parts/fabrications/{fid}/image").status_code == 200


def test_delete_commit_failure_keeps_record_and_image(app, super_admin_client, monkeypatch):
    client, part = super_admin_client
    fid = _create(client, part["id"], image=(io.BytesIO(_png_bytes()), "p.png")).get_json()["id"]
    files_before = _uploaded_files(app)

    _break_next_commit(monkeypatch)
    res = client.delete(f"/api/spare-parts/fabrications/{fid}")
    assert res.status_code == 500

    with app.app_context():
        assert db.session.get(SparePartFabrication, fid) is not None  # record remains
    assert _uploaded_files(app) == files_before  # image remains
    assert client.get(f"/api/spare-parts/fabrications/{fid}/image").status_code == 200


def test_successful_image_replacement_deletes_old_image_only_after_commit(app, super_admin_client):
    client, part = super_admin_client
    fid = _create(client, part["id"], image=(io.BytesIO(_png_bytes((255, 0, 0))), "old.png")).get_json()["id"]
    with app.app_context():
        old_image_path = db.session.get(SparePartFabrication, fid).image_path
    assert old_image_path in _uploaded_files(app)

    res = client.patch(f"/api/spare-parts/fabrications/{fid}",
                        data={"image": (io.BytesIO(_png_bytes((0, 255, 0))), "new.png")},
                        content_type="multipart/form-data")
    assert res.status_code == 200
    with app.app_context():
        new_image_path = db.session.get(SparePartFabrication, fid).image_path
    assert new_image_path != old_image_path

    files_after = _uploaded_files(app)
    assert new_image_path in files_after      # new image exists
    assert old_image_path not in files_after  # old image removed, only after the commit succeeded


def test_successful_image_removal_deletes_old_image_after_commit(app, super_admin_client):
    client, part = super_admin_client
    fid = _create(client, part["id"], image=(io.BytesIO(_png_bytes()), "p.png")).get_json()["id"]
    with app.app_context():
        old_image_path = db.session.get(SparePartFabrication, fid).image_path

    res = client.patch(f"/api/spare-parts/fabrications/{fid}",
                        data={"remove_image": "1"}, content_type="multipart/form-data")
    assert res.status_code == 200
    with app.app_context():
        assert db.session.get(SparePartFabrication, fid).image_path is None
    assert old_image_path not in _uploaded_files(app)


def test_successful_delete_removes_record_and_image_after_commit(app, super_admin_client):
    client, part = super_admin_client
    fid = _create(client, part["id"], image=(io.BytesIO(_png_bytes()), "p.png")).get_json()["id"]
    with app.app_context():
        image_path = db.session.get(SparePartFabrication, fid).image_path
    assert image_path in _uploaded_files(app)

    res = client.delete(f"/api/spare-parts/fabrications/{fid}")
    assert res.status_code == 200
    with app.app_context():
        assert db.session.get(SparePartFabrication, fid) is None
    assert image_path not in _uploaded_files(app)


def test_post_commit_old_image_cleanup_failure_does_not_undo_committed_change(app, super_admin_client, monkeypatch):
    client, part = super_admin_client
    fid = _create(client, part["id"], image=(io.BytesIO(_png_bytes((255, 0, 0))), "old.png")).get_json()["id"]

    def _raise_remove(path):
        raise OSError("simulated disk error removing old file")
    monkeypatch.setattr(os, "remove", _raise_remove)

    res = client.patch(f"/api/spare-parts/fabrications/{fid}",
                        data={"image": (io.BytesIO(_png_bytes((0, 0, 255))), "new.png")},
                        content_type="multipart/form-data")
    assert res.status_code == 200  # the DB write succeeded despite the cleanup failure
    assert res.get_json()["has_image"] is True
    with app.app_context():
        fab = db.session.get(SparePartFabrication, fid)
        assert fab.image_path is not None  # new image reference committed successfully


def test_missing_image_file_returns_404_not_500(app, super_admin_client):
    client, part = super_admin_client
    fid = _create(client, part["id"], image=(io.BytesIO(_png_bytes()), "p.png")).get_json()["id"]
    with app.app_context():
        path = svc.image_file_path(db.session.get(SparePartFabrication, fid))
    os.remove(path)  # simulate the file having gone missing out-of-band

    res = client.get(f"/api/spare-parts/fabrications/{fid}/image")
    assert res.status_code == 404


def test_non_super_admin_image_access_remains_403_after_lifecycle_fix(app, make_user, super_admin_client):
    sa_client, part = super_admin_client
    fid = _create(sa_client, part["id"], image=(io.BytesIO(_png_bytes()), "p.png")).get_json()["id"]
    mgr_client = app.test_client()
    _login_spare_parts(mgr_client, make_user, "fab_lifecycle_mgr", "manager")
    assert mgr_client.get(f"/api/spare-parts/fabrications/{fid}/image").status_code == 403
