"""
Spare Part Fabrication — Super-Admin-only financial history (cost vs.
amount sold to the factory). See
webapp/services/spare_part_fabrication_service.py and
webapp/routes/spare_part_fabrications.py.
"""
import io

import pytest
from PIL import Image

from webapp.extensions import db
from webapp.models.spare_part_fabrication import SparePartFabrication

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
