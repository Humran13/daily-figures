"""China/Local source pricing and safe legacy-cost migration."""
import sqlite3
from decimal import Decimal

import pytest

from webapp.extensions import db
from webapp.models.spare_part import SparePart, current_cost


def _login(client, make_user, username="source_price_admin", role="super_admin"):
    from tests.test_spare_parts import _login_spare_parts
    return _login_spare_parts(client, make_user, username, role)


def _part(client, name="Source-priced Bearing", specifications="22213"):
    response = client.post("/api/spare-parts", json={
        "name": name, "specifications": specifications, "unit": "pcs",
    })
    assert response.status_code == 201, response.get_json()
    return response.get_json()


@pytest.mark.parametrize(
    ("payload", "expected_source", "expected_cost"),
    [
        ({"china_buying_price": "85000", "selling_price": "150000", "preferred_cost_source": "china"}, "china", "85000.00"),
        ({"local_buying_price": "120000", "selling_price": "150000", "preferred_cost_source": "local_uganda"}, "local_uganda", "120000.00"),
        ({"china_buying_price": "85000", "local_buying_price": "120000", "selling_price": "150000", "preferred_cost_source": "china"}, "china", "85000.00"),
    ],
)
def test_source_price_combinations_can_be_saved(client, make_user, payload, expected_source, expected_cost):
    _login(client, make_user, username=f"source_{expected_source}_{len(payload)}")
    part = _part(client, name=f"Combination {expected_source} {len(payload)}")
    response = client.patch(f"/api/admin/spare-parts/pricing/{part['id']}", json=payload)
    assert response.status_code == 200, response.get_json()
    row = response.get_json()
    assert row["preferred_cost_source"] == expected_source
    assert row["current_cost_price"] == expected_cost


def test_calculations_and_preferred_source_switch(client, make_user):
    _login(client, make_user, username="source_calc_admin")
    part = _part(client)
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})

    response = client.patch(f"/api/admin/spare-parts/pricing/{part['id']}", json={
        "china_buying_price": "85000", "local_buying_price": "120000",
        "selling_price": "150000", "preferred_cost_source": "china",
    })
    assert response.status_code == 200, response.get_json()
    row = response.get_json()
    expected = {
        "current_cost_price": "85000.00", "china_profit_per_unit": "65000.00",
        "local_profit_per_unit": "30000.00", "buying_cost_difference": "35000.00",
        "current_stock_value": "850000.00", "expected_sales_value": "1500000.00",
        "expected_gross_profit": "650000.00",
    }
    for field, value in expected.items():
        assert Decimal(row[field]) == Decimal(value), field
    assert row["cheaper_source"] == "china"

    response = client.patch(f"/api/admin/spare-parts/pricing/{part['id']}", json={
        "preferred_cost_source": "local_uganda",
    })
    row = response.get_json()
    assert Decimal(row["current_cost_price"]) == Decimal("120000")
    assert Decimal(row["current_stock_value"]) == Decimal("1200000")
    assert Decimal(row["expected_gross_profit"]) == Decimal("300000")


def test_selling_price_can_be_updated_independently(client, make_user):
    _login(client, make_user, username="selling_update_admin")
    part = _part(client, name="Selling Update")
    client.patch(f"/api/admin/spare-parts/pricing/{part['id']}", json={
        "china_buying_price": "100", "selling_price": "150", "preferred_cost_source": "china",
    })
    response = client.patch(f"/api/admin/spare-parts/pricing/{part['id']}", json={"selling_price": "175"})
    assert response.status_code == 200
    assert Decimal(response.get_json()["selling_price"]) == Decimal("175")
    assert Decimal(response.get_json()["profit_per_unit"]) == Decimal("75")


@pytest.mark.parametrize("payload", [
    {"china_buying_price": "-1"},
    {"local_buying_price": "-1"},
    {"selling_price": "-1"},
    {"preferred_cost_source": "overseas"},
    {"preferred_cost_source": "china"},
    {"preferred_cost_source": "local_uganda"},
    {"china_buying_price": "0", "preferred_cost_source": "china"},
    {"local_buying_price": "0", "preferred_cost_source": "local_uganda"},
])
def test_invalid_source_pricing_is_rejected(client, make_user, payload):
    _login(client, make_user, username="source_validation_admin")
    part = _part(client, name="Validation Part")
    response = client.patch(f"/api/admin/spare-parts/pricing/{part['id']}", json=payload)
    assert response.status_code == 400


def test_local_validation_uses_the_display_label(client, make_user):
    _login(client, make_user, username="local_validation_label_admin")
    part = _part(client, name="Local Validation Label")
    response = client.patch(f"/api/admin/spare-parts/pricing/{part['id']}", json={
        "preferred_cost_source": "local_uganda",
    })
    assert response.status_code == 400
    assert "Local Buying Price" in response.get_json()["error"]
    assert "Local Uganda" not in response.get_json()["error"]


def test_source_prices_and_source_changes_use_existing_audit_history(client, make_user):
    _login(client, make_user, username="source_history_admin")
    part = _part(client, name="History Part")
    client.patch(f"/api/admin/spare-parts/pricing/{part['id']}", json={
        "china_buying_price": "10", "local_buying_price": "12",
        "selling_price": "20", "preferred_cost_source": "china",
    })
    history = client.get(f"/api/admin/spare-parts/pricing/{part['id']}/history").get_json()
    assert len(history) == 1
    assert history[0]["username"] == "source_history_admin"
    assert set(history[0]["after"]) == {
        "china_buying_price", "local_buying_price", "selling_price", "preferred_cost_source",
    }
    assert history[0]["before"]["preferred_cost_source"] is None


def test_source_pricing_permissions_and_specification_search(client, make_user):
    _login(client, make_user, username="source_search_admin")
    part = _part(client)
    assert [r["id"] for r in client.get(
        "/api/admin/spare-parts/pricing", query_string={"q": "22213"},
    ).get_json()["rows"]] == [part["id"]]

    _login(client, make_user, username="source_price_manager", role="manager")
    assert client.get("/api/admin/spare-parts/pricing").status_code == 403
    assert client.patch(f"/api/admin/spare-parts/pricing/{part['id']}", json={
        "china_buying_price": "1", "preferred_cost_source": "china",
    }).status_code == 403


def test_admin_pricing_page_exposes_source_fields_and_comparison(client, make_user):
    _login(client, make_user, username="source_page_admin")
    html = client.get("/admin.html").get_data(as_text=True)
    for label in (
        "China Buying Price", "Local Buying Price", "Preferred Cost Source",
        "Current cost", "Current stock value", "Source comparison",
    ):
        assert label in html
    assert 'id="spPricingModal"' in html


def test_master_create_saves_both_prices_and_syncs_admin_pricing(client, make_user):
    _login(client, make_user, username="master_source_create_admin")
    response = client.post("/api/spare-parts", json={
        "name": "Master Source Part", "specifications": "Both prices", "unit": "pcs",
        "china_buying_price": "85000", "local_buying_price": "120000",
        "selling_price": "150000", "preferred_cost_source": "china",
    })
    assert response.status_code == 201, response.get_json()
    created = response.get_json()
    assert Decimal(created["china_buying_price"]) == Decimal("85000")
    assert Decimal(created["local_buying_price"]) == Decimal("120000")
    assert created["preferred_cost_source"] == "china"
    assert Decimal(created["current_cost_price"]) == Decimal("85000")

    rows = client.get("/api/admin/spare-parts/pricing", query_string={"q": "Master Source Part"}).get_json()["rows"]
    assert len(rows) == 1
    assert Decimal(rows[0]["china_buying_price"]) == Decimal("85000")
    assert Decimal(rows[0]["local_buying_price"]) == Decimal("120000")
    assert rows[0]["preferred_cost_source"] == "china"


def test_master_partial_price_edits_preserve_the_other_source_and_audit(client, make_user):
    _login(client, make_user, username="master_source_edit_admin")
    part = client.post("/api/spare-parts", json={
        "name": "Master Edit Part", "unit": "pcs", "china_buying_price": "85000",
        "local_buying_price": "120000", "preferred_cost_source": "china",
    }).get_json()

    response = client.patch(f"/api/spare-parts/{part['id']}", json={"china_buying_price": "90000"})
    assert response.status_code == 200, response.get_json()
    edited = response.get_json()
    assert Decimal(edited["china_buying_price"]) == Decimal("90000")
    assert Decimal(edited["local_buying_price"]) == Decimal("120000")

    response = client.patch(f"/api/spare-parts/{part['id']}", json={"local_buying_price": "125000"})
    edited = response.get_json()
    assert Decimal(edited["china_buying_price"]) == Decimal("90000")
    assert Decimal(edited["local_buying_price"]) == Decimal("125000")

    history = client.get(f"/api/admin/spare-parts/pricing/{part['id']}/history").get_json()
    assert history[0]["before"] == {"local_buying_price": "120000.00"}
    assert history[0]["after"] == {"local_buying_price": "125000"}
    assert history[1]["before"] == {"china_buying_price": "85000.00"}
    assert history[1]["after"] == {"china_buying_price": "90000"}


def test_master_switches_source_without_changing_saved_prices(client, make_user):
    _login(client, make_user, username="master_source_switch_admin")
    part = client.post("/api/spare-parts", json={
        "name": "Master Switch Part", "unit": "pcs", "china_buying_price": "90000",
        "local_buying_price": "120000", "preferred_cost_source": "china",
    }).get_json()
    response = client.patch(f"/api/spare-parts/{part['id']}", json={
        "preferred_cost_source": "local_uganda",
    })
    assert response.status_code == 200, response.get_json()
    edited = response.get_json()
    assert Decimal(edited["current_cost_price"]) == Decimal("120000")
    assert Decimal(edited["china_buying_price"]) == Decimal("90000")
    assert Decimal(edited["local_buying_price"]) == Decimal("120000")


@pytest.mark.parametrize(("price_field", "source"), [
    ("china_buying_price", "china"),
    ("local_buying_price", "local_uganda"),
])
def test_master_create_accepts_only_one_known_source_price(client, make_user, price_field, source):
    _login(client, make_user, username=f"master_one_source_{source}")
    response = client.post("/api/spare-parts", json={
        "name": f"Only {source}", "unit": "pcs", price_field: "85000",
        "preferred_cost_source": source,
    })
    assert response.status_code == 201, response.get_json()
    assert Decimal(response.get_json()[price_field]) == Decimal("85000")


def test_master_rejects_unpriced_preferred_source(client, make_user):
    _login(client, make_user, username="master_invalid_source_admin")
    response = client.post("/api/spare-parts", json={
        "name": "Invalid Preferred Source", "unit": "pcs", "china_buying_price": "85000",
        "preferred_cost_source": "local_uganda",
    })
    assert response.status_code == 400
    assert "Local Buying Price" in response.get_json()["error"]


def test_master_source_prices_are_hidden_and_protected_for_manager(app, client, make_user):
    owner = app.test_client()
    _login(owner, make_user, username="master_price_owner")
    part = owner.post("/api/spare-parts", json={
        "name": "Protected Source Prices", "unit": "pcs", "china_buying_price": "10",
        "local_buying_price": "12", "preferred_cost_source": "china",
    }).get_json()

    _login(client, make_user, username="master_price_manager", role="manager")
    visible = client.get(f"/api/spare-parts/{part['id']}").get_json()
    for field in ("china_buying_price", "local_buying_price", "preferred_cost_source", "current_cost_price"):
        assert field not in visible
    assert client.patch(f"/api/spare-parts/{part['id']}", json={
        "local_buying_price": "15",
    }).status_code == 403


def test_master_page_uses_independent_source_price_fields(client, make_user):
    _login(client, make_user, username="master_source_page_admin")
    html = client.get("/spare-parts-master.html").get_data(as_text=True)
    for text in ("China Buying Price", "Local Buying Price", "Preferred Cost Source"):
        assert text in html
    for field in ("china_buying_price", "local_buying_price", "preferred_cost_source"):
        assert f'data-key="{field}"' in html
    assert 'id="spChinaBuyingPrice"' in html
    assert 'id="spLocalBuyingPrice"' in html
    assert 'id="spPreferredCostSource"' in html
    assert "Local Uganda" not in html
    assert "spBuyingPrice" not in html


def test_legacy_buying_price_remains_current_until_source_selected(app, client, make_user):
    _login(client, make_user, username="legacy_cost_admin")
    response = client.post("/api/spare-parts", json={
        "name": "Legacy Cost Part", "unit": "pcs", "buying_price": "40000", "selling_price": "55000",
    })
    part_id = response.get_json()["id"]
    with app.app_context():
        part = db.session.get(SparePart, part_id)
        assert current_cost(part) == Decimal("40000")
        assert part.china_buying_price is None and part.local_buying_price is None
        assert part.preferred_cost_source is None


def test_migration_preserves_unknown_legacy_buying_price(tmp_path, monkeypatch):
    from flask_migrate import upgrade
    from webapp import create_app

    db_path = tmp_path / "source_price_migration.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("SUPERADMIN_USERNAME", raising=False)
    monkeypatch.delenv("SUPERADMIN_PASSWORD", raising=False)
    flask_app = create_app()
    with flask_app.app_context():
        upgrade(revision="a7c9d1e3f5b8")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO spare_parts (code,name,unit,buying_price,selling_price,active,current_stock_cache,created_at,updated_at) "
        "VALUES ('SP-99999','Migrated Legacy','pcs',40000,55000,1,7,'2026-10-01','2026-10-01')"
    )
    conn.commit()
    conn.close()

    with flask_app.app_context():
        upgrade()
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT buying_price, china_buying_price, local_buying_price, preferred_cost_source, current_stock_cache "
        "FROM spare_parts WHERE code='SP-99999'"
    ).fetchone()
    conn.close()
    assert row == (40000, None, None, None, 7)
