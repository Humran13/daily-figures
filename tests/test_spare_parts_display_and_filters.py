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
    healthy_rows = client.get("/api/spare-parts", query_string={"stock": "healthy"}).get_json()
    no_stock_rows = client.get("/api/spare-parts", query_string={"stock": "no_stock"}).get_json()
    stocked_rows = client.get("/api/spare-parts", query_string={"stock": "in_stock"}).get_json()

    # Low, healthy, and no-stock are mutually exclusive inventory states: a
    # part at zero is out of stock even when it has a minimum threshold.
    assert dashboard["low_stock_count"] == len(low_rows) == 1
    assert dashboard["healthy_stock_count"] == len(healthy_rows) == 1
    assert dashboard["no_stock_count"] == len(no_stock_rows) == 2
    assert {row["id"] for row in dashboard["low_stock"]} == {bearing["id"]}
    assert {row["id"] for row in low_rows} == {bearing["id"]}
    assert {row["id"] for row in healthy_rows} == {chain_stocked["id"]}
    assert {row["id"] for row in no_stock_rows} == {chain_zero["id"], no_threshold["id"]}
    assert {row["display_name"] for row in low_rows} == {"Bearing [6204]"}

    assert dashboard["current_stock_items"] == len(stocked_rows) == 2
    assert {row["id"] for row in stocked_rows} == {chain_stocked["id"], bearing["id"]}
    assert inactive["id"] not in {
        row["id"] for row in low_rows + healthy_rows + no_stock_rows + stocked_rows
    }


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
    assert 'href="/spare-parts.html?stock=healthy#inventory"' in html
    assert 'href="/spare-parts.html?stock=low#inventory"' in html
    assert 'href="/spare-parts.html?stock=no_stock#inventory"' in html
    assert 'aria-label="View healthy-stock items"' in html
    assert 'aria-label="View low-stock items"' in html
    assert 'aria-label="View out-of-stock items"' in html
    assert "'Inventory — ' + STOCK_FILTER_LABELS[stock]" in html
    assert "healthy:'Healthy Stock'" in html
    assert "low:'Low Stock'" in html
    assert "no_stock:'No Stock'" in html
    assert "new URLSearchParams(window.location.search).get('stock')" in html
    assert "setInventoryUrl(initialStock, 'replace')" in html
    assert "if(requestId !== inventoryRequestId) return;" in html
    assert "inventoryRequestId++;" in html


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


def test_stock_movement_date_defaults_use_server_business_today(client, make_user):
    """Movement dates must anchor on the Africa/Kampala business day, not the
    browser clock (a device in another timezone could otherwise post to the
    wrong day)."""
    _login_spare_parts(client, make_user, "movement_date_mgr", "manager")
    helper = "function todayStr(){ return businessToday || new Date().toISOString().slice(0,10); }"
    for page in (
        "/spare-parts-master.html",
        "/spare-parts-stock-in.html",
        "/spare-parts-stock-out.html",
    ):
        html = client.get(page).get_data(as_text=True)
        assert helper in html, page
        assert "businessToday = s.business_today || null;" in html, page


def test_stock_count_ui_posts_an_auditable_physical_count(client, make_user):
    """The Master page must expose the count workflow, not just the API."""
    _login_spare_parts(client, make_user, "count_ui_mgr", "manager")
    html = client.get("/spare-parts-master.html").get_data(as_text=True)
    assert 'id="countSpareInput"' in html
    assert 'id="countQty"' in html
    assert 'id="countReason"' in html
    assert 'id="submitCountBtn"' in html
    assert "/api/spare-parts/movements/stock-count" in html
    assert "counted_quantity" in html
    assert "A reason is required for a stock count correction." in html
    assert "SparePartsUI.withBusy(submitCountBtn" in html
