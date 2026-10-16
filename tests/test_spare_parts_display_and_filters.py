"""Focused coverage for spare-part presentation identity and stock navigation."""

from webapp.models.spare_part import spare_part_display_name

from tests.test_spare_parts import _create_spare_part, _login_spare_parts


def test_spare_part_display_name_formats_specifications_without_empty_brackets():
    assert spare_part_display_name("Bearing", "6204") == "Bearing [6204]"
    assert spare_part_display_name("Bearing", "") == "Bearing"
    assert spare_part_display_name("Bearing", None) == "Bearing"
    assert spare_part_display_name("Bearing", "   ") == "Bearing"
    assert spare_part_display_name("Oil Seal", "35x52x7") == "Oil Seal [35x52x7]"
    assert {
        spare_part_display_name("Bearing", "6204"),
        spare_part_display_name("Bearing", "6205"),
    } == {"Bearing [6204]", "Bearing [6205]"}


def test_api_preserves_raw_identity_and_specification_search(client, make_user):
    _login_spare_parts(client, make_user, "display_mgr", "manager")
    part = _create_spare_part(client, name="Bearing", specifications="6204")

    assert part["name"] == "Bearing"
    assert part["specifications"] == "6204"
    assert part["display_name"] == "Bearing [6204]"

    rows = client.get("/api/spare-parts", query_string={"q": "6204"}).get_json()
    assert [row["id"] for row in rows] == [part["id"]]
    assert rows[0]["display_name"] == "Bearing [6204]"


def test_dashboard_counts_and_stock_filters_use_same_active_inventory(client, make_user):
    _login_spare_parts(client, make_user, "filter_mgr", "manager")
    chain_zero = _create_spare_part(
        client, name="Industrial Roller Chain", specifications="08B-1", minimum_stock="3",
    )
    chain_stocked = _create_spare_part(
        client, name="Industrial Roller Chain", specifications="12B-1", minimum_stock="3",
    )
    bearing = _create_spare_part(
        client, name="Bearing", specifications="6204", minimum_stock="5",
    )
    no_threshold = _create_spare_part(client, name="Unmonitored", specifications="X")
    inactive = _create_spare_part(
        client, name="Archived Bearing", specifications="9999", minimum_stock="10",
    )

    client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": chain_stocked["id"], "quantity": "5",
    })
    client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": bearing["id"], "quantity": "1",
    })
    client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": inactive["id"], "quantity": "2",
    })
    client.patch(f"/api/spare-parts/{inactive['id']}", json={"active": False})

    dashboard = client.get("/api/spare-parts/dashboard").get_json()
    low_rows = client.get("/api/spare-parts", query_string={"stock": "low"}).get_json()
    stocked_rows = client.get("/api/spare-parts", query_string={"stock": "in_stock"}).get_json()

    assert dashboard["low_stock_count"] == len(low_rows) == 2
    assert {row["id"] for row in dashboard["low_stock"]} == {
        chain_zero["id"], bearing["id"],
    }
    assert {row["id"] for row in low_rows} == {chain_zero["id"], bearing["id"]}
    assert {row["display_name"] for row in low_rows} == {
        "Industrial Roller Chain [08B-1]", "Bearing [6204]",
    }

    assert dashboard["current_stock_items"] == len(stocked_rows) == 2
    assert {row["id"] for row in stocked_rows} == {chain_stocked["id"], bearing["id"]}
    assert no_threshold["id"] not in {row["id"] for row in low_rows}
    assert inactive["id"] not in {row["id"] for row in low_rows + stocked_rows}


def test_filtered_inventory_remains_readable_without_pricing_permission(client, make_user):
    _login_spare_parts(client, make_user, "filter_viewer", "viewer")
    response = client.get("/api/spare-parts", query_string={"stock": "low"})
    assert response.status_code == 200
    for row in response.get_json():
        assert "display_name" in row
        assert "buying_price" not in row
        assert "selling_price" not in row
        assert "profit" not in row


def test_dashboard_cards_are_semantic_links_to_visible_filters(client, make_user):
    _login_spare_parts(client, make_user, "dashboard_links", "viewer")
    html = client.get("/spare-parts.html").get_data(as_text=True)
    assert 'href="/spare-parts.html?stock=low#inventory"' in html
    assert 'href="/spare-parts.html?stock=in_stock#inventory"' in html
    assert 'aria-label="View low-stock items"' in html
    assert 'aria-label="View items with stock"' in html
    assert "Inventory — Low Stock" in html
    assert "Inventory — Items With Stock" in html


def test_history_and_admin_pricing_use_display_identity(client, make_user):
    _login_spare_parts(client, make_user, "display_admin", "super_admin")
    part = _create_spare_part(client, name="Oil Seal", specifications="35x52x7")
    client.post("/api/spare-parts/movements/stock-in", json={
        "spare_part_id": part["id"], "quantity": "1",
    })

    history = client.get("/api/spare-parts/movements").get_json()
    movement = next(row for row in history if row["spare_part_id"] == part["id"])
    assert movement["spare_part_display_name"] == "Oil Seal [35x52x7]"

    pricing = client.get("/api/admin/spare-parts/pricing").get_json()["rows"]
    row = next(row for row in pricing if row["id"] == part["id"])
    assert row["display_name"] == "Oil Seal [35x52x7]"
