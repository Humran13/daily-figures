"""Regression coverage for the Dashboard inventory drill-down's category and
text-search filters (layered on top of the existing stock-status filter)."""

from tests.test_spare_parts import _create_spare_part, _login_spare_parts


def _create_category(client, name):
    res = client.post("/api/spare-parts/categories", json={"name": name})
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def test_stock_status_cards_each_load_matching_items(client, make_user):
    _login_spare_parts(client, make_user, "stock_mgr", "manager")
    healthy = _create_spare_part(client, name="Healthy Chain", minimum_stock="3")
    low = _create_spare_part(client, name="Low Bearing", minimum_stock="5")
    none = _create_spare_part(client, name="Empty Seal", minimum_stock="2")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": healthy["id"], "quantity": "10"})
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": low["id"], "quantity": "1"})

    healthy_rows = client.get("/api/spare-parts", query_string={"stock": "healthy"}).get_json()
    low_rows = client.get("/api/spare-parts", query_string={"stock": "low"}).get_json()
    no_stock_rows = client.get("/api/spare-parts", query_string={"stock": "no_stock"}).get_json()

    assert {r["id"] for r in healthy_rows} == {healthy["id"]}
    assert {r["id"] for r in low_rows} == {low["id"]}
    assert {r["id"] for r in no_stock_rows} == {none["id"]}


def test_categories_endpoint_lists_active_categories_alphabetically(client, make_user):
    _login_spare_parts(client, make_user, "cat_mgr", "manager")
    _create_category(client, "Seals")
    _create_category(client, "Bearings")
    inactive = _create_category(client, "Retired")
    client.patch(f"/api/spare-parts/categories/{inactive['id']}", json={"active": False})

    names = [c["name"] for c in client.get("/api/spare-parts/categories").get_json()]
    assert names == sorted(names)
    assert "Retired" not in names
    assert {"Seals", "Bearings"} <= set(names)


def test_category_combines_with_each_stock_status(client, make_user):
    _login_spare_parts(client, make_user, "combo_mgr", "manager")
    bearings = _create_category(client, "Bearings")
    seals = _create_category(client, "Seals")

    healthy_bearing = _create_spare_part(client, name="6204 Bearing", category_id=bearings["id"], minimum_stock="2")
    low_bearing = _create_spare_part(client, name="6205 Bearing", category_id=bearings["id"], minimum_stock="5")
    no_stock_bearing = _create_spare_part(client, name="6206 Bearing", category_id=bearings["id"], minimum_stock="2")
    healthy_seal = _create_spare_part(client, name="Oil Seal", category_id=seals["id"], minimum_stock="2")

    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": healthy_bearing["id"], "quantity": "10"})
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": low_bearing["id"], "quantity": "1"})
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": healthy_seal["id"], "quantity": "10"})

    no_stock_bearings = client.get("/api/spare-parts", query_string={
        "stock": "no_stock", "category_id": bearings["id"],
    }).get_json()
    assert {r["id"] for r in no_stock_bearings} == {no_stock_bearing["id"]}

    healthy_bearings = client.get("/api/spare-parts", query_string={
        "stock": "healthy", "category_id": bearings["id"],
    }).get_json()
    assert {r["id"] for r in healthy_bearings} == {healthy_bearing["id"]}
    assert healthy_seal["id"] not in {r["id"] for r in healthy_bearings}

    low_bearings = client.get("/api/spare-parts", query_string={
        "stock": "low", "category_id": bearings["id"],
    }).get_json()
    assert {r["id"] for r in low_bearings} == {low_bearing["id"]}


def test_search_combines_with_stock_and_category(client, make_user):
    _login_spare_parts(client, make_user, "search_mgr", "manager")
    bearings = _create_category(client, "Bearings")
    match = _create_spare_part(client, name="Bearing", specifications="6203", category_id=bearings["id"], minimum_stock="2")
    other_spec = _create_spare_part(client, name="Bearing", specifications="6300", category_id=bearings["id"], minimum_stock="2")
    other_category = _create_spare_part(client, name="6203 Belt", minimum_stock="2")

    search_only = client.get("/api/spare-parts", query_string={"stock": "no_stock", "q": "6203"}).get_json()
    assert {r["id"] for r in search_only} == {match["id"], other_category["id"]}

    combined = client.get("/api/spare-parts", query_string={
        "stock": "no_stock", "category_id": bearings["id"], "q": "6203",
    }).get_json()
    assert {r["id"] for r in combined} == {match["id"]}
    assert other_spec["id"] not in {r["id"] for r in combined}
    assert other_category["id"] not in {r["id"] for r in combined}


def test_all_categories_option_removes_only_category_filtering(client, make_user):
    _login_spare_parts(client, make_user, "allcat_mgr", "manager")
    category = _create_category(client, "Motors")
    in_cat = _create_spare_part(client, name="Motor Part", category_id=category["id"])
    out_cat = _create_spare_part(client, name="Other Part")

    filtered = client.get("/api/spare-parts", query_string={"category_id": category["id"]}).get_json()
    unfiltered = client.get("/api/spare-parts").get_json()

    assert {r["id"] for r in filtered} == {in_cat["id"]}
    assert {in_cat["id"], out_cat["id"]} <= {r["id"] for r in unfiltered}


def test_clearing_search_removes_only_text_filtering(client, make_user):
    _login_spare_parts(client, make_user, "clearsearch_mgr", "manager")
    part = _create_spare_part(client, name="Unique Widget Alpha")

    found = client.get("/api/spare-parts", query_string={"q": "Alpha"}).get_json()
    cleared = client.get("/api/spare-parts").get_json()

    assert {r["id"] for r in found} == {part["id"]}
    assert part["id"] in {r["id"] for r in cleared}


def test_combined_filters_hide_financial_fields_from_viewer(client, make_user):
    _login_spare_parts(client, make_user, "finviewer", "viewer")
    rows = client.get("/api/spare-parts", query_string={"stock": "low", "category_id": "1", "q": "x"}).get_json()
    for row in rows:
        assert "buying_price" not in row
        assert "selling_price" not in row
        assert "profit" not in row
        assert "china_buying_price" not in row
        assert "local_buying_price" not in row


def test_dashboard_inventory_markup_includes_category_and_search_controls(client, make_user):
    _login_spare_parts(client, make_user, "markup_viewer", "viewer")
    html = client.get("/spare-parts.html").get_data(as_text=True)

    assert 'id="inventorySearch"' in html
    assert 'placeholder="Search spare part..."' in html
    assert 'id="inventoryCategoryFilter"' in html
    assert 'id="resetInventoryFilters"' in html
    assert 'id="clearInventoryFilter"' in html
    assert "/api/spare-parts/categories" in html
    assert "No matching spare parts." in html
    assert "Inventory could not be loaded. Please try again." in html


def test_dashboard_state_preserves_category_and_search_across_card_switches(client, make_user):
    _login_spare_parts(client, make_user, "state_viewer", "viewer")
    html = client.get("/spare-parts.html").get_data(as_text=True)

    # loadFilteredInventory (invoked for every card click) must not reset
    # the secondary filters, only clearInventoryFilter should.
    assert "selectedStockFilter = stock;" in html
    # clearInventoryFilter resets both secondary filters; loadFilteredInventory
    # (run on every card click, including switches) must not reset either.
    import re
    load_fn = re.search(r"async function loadFilteredInventory\(stock, options\)\{.*?\n\}", html, re.S).group(0)
    clear_fn = re.search(r"function clearInventoryFilter\(options\)\{.*?\n\}", html, re.S).group(0)
    assert "selectedCategoryId = ''" not in load_fn
    assert "searchText = ''" not in load_fn
    assert "selectedCategoryId = '';" in clear_fn
    assert "searchText = '';" in clear_fn


def test_dashboard_debounces_search_input(client, make_user):
    _login_spare_parts(client, make_user, "debounce_viewer", "viewer")
    html = client.get("/spare-parts.html").get_data(as_text=True)
    assert "clearTimeout(inventorySearchDebounce);" in html
    assert "}, 250);" in html


def test_dashboard_unrelated_sections_remain_unchanged(client, make_user):
    _login_spare_parts(client, make_user, "sections_viewer", "viewer")
    html = client.get("/spare-parts.html").get_data(as_text=True)
    assert "Most Taken Out Spare Parts" in html
    assert "Recent Movements" in html
    assert 'id="mostIssuedList"' in html
    assert 'id="recentMovementsList"' in html
    assert ".inventory-row.healthy .stock{ color:var(--teal); }" in html
    assert ".inventory-row.low .stock, .inventory-row.no_stock .stock{ color:var(--red); }" in html
