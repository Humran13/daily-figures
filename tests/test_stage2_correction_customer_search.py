"""
Stage 2 correction: server-side customer/recipient search for
GET /api/dispatches (and, since it shares _filtered_dispatch_query,
GET /api/dispatches/export.<fmt>), replacing history.html's old
client-side substring filter that only ever saw the currently loaded page.
"""
import pytest


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Search Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Search Category"}).get_json()
    return {"product": product, "category": category}


def _dispatch(client, product_id, customer_id, number, date="2026-07-28"):
    return client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date, "shift": "Day", "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()


def test_customer_beyond_the_first_page_is_still_found(client, setup):
    pid = setup["product"]["id"]
    cat_id = setup["category"]["id"]
    target = client.post("/api/admin/customers", json={"name": "Zzz Target Customer", "sales_category_id": cat_id}).get_json()
    # Push several unrelated dispatches in front of the target so it would
    # fall off a small unfiltered "page 1".
    for i in range(5):
        other = client.post("/api/admin/customers", json={
            "name": f"Filler {i}", "sales_category_id": cat_id, "confirm_not_duplicate": True,
        }).get_json()
        _dispatch(client, pid, other["id"], f"PAGE-{i}", date="2026-07-29")
    _dispatch(client, pid, target["id"], "PAGE-TARGET", date="2026-07-01")  # oldest -> last without filtering

    res = client.get("/api/dispatches?customer_name=Zzz Target&limit=2")
    data = res.get_json()
    assert data["total"] == 1
    assert data["results"][0]["dispatch_number"] == "PAGE-TARGET"


def test_customer_search_is_case_insensitive(client, setup):
    pid = setup["product"]["id"]
    cust = client.post("/api/admin/customers", json={
        "name": "Kenjoy Supermarket", "sales_category_id": setup["category"]["id"],
    }).get_json()
    _dispatch(client, pid, cust["id"], "CASE-1")

    res = client.get("/api/dispatches?customer_name=kenjoy SUPERMARKET")
    assert res.get_json()["total"] == 1


def test_customer_search_supports_partial_match(client, setup):
    pid = setup["product"]["id"]
    cust = client.post("/api/admin/customers", json={
        "name": "Carrefour Oasis Mall", "sales_category_id": setup["category"]["id"],
    }).get_json()
    _dispatch(client, pid, cust["id"], "PARTIAL-1")

    res = client.get("/api/dispatches?customer_name=Oasis")
    assert res.get_json()["total"] == 1


def test_temporary_customer_name_is_searchable(client, setup):
    pid = setup["product"]["id"]
    res = client.post("/api/dispatches", json={
        "dispatch_number": "TEMP-1", "date": "2026-07-28", "shift": "Day",
        "sales_category_id": setup["category"]["id"],
        "new_customer_name": "Walk-in Unverified Buyer",
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 201

    found = client.get("/api/dispatches?customer_name=Walk-in Unverified")
    assert found.get_json()["total"] == 1


def test_historical_snapshot_name_is_searchable_after_customer_renamed(client, setup):
    pid = setup["product"]["id"]
    cust = client.post("/api/admin/customers", json={
        "name": "Old Name Inc", "sales_category_id": setup["category"]["id"],
    }).get_json()
    _dispatch(client, pid, cust["id"], "RENAME-1")

    rename = client.patch(f"/api/admin/customers/{cust['id']}", json={"name": "New Name LLC"})
    assert rename.status_code == 200

    # Findable both by the name recorded at the time (snapshot)...
    by_snapshot = client.get("/api/dispatches?customer_name=Old Name")
    assert by_snapshot.get_json()["total"] == 1
    # ...and by the customer's current live name.
    by_current = client.get("/api/dispatches?customer_name=New Name")
    assert by_current.get_json()["total"] == 1


def test_customer_search_combines_with_other_filters(client, setup):
    pid = setup["product"]["id"]
    cust = client.post("/api/admin/customers", json={
        "name": "Combo Customer", "sales_category_id": setup["category"]["id"],
    }).get_json()
    d1 = _dispatch(client, pid, cust["id"], "COMBO-DRAFT")
    d2 = _dispatch(client, pid, cust["id"], "COMBO-FINAL")
    client.post(f"/api/dispatches/{d2['id']}/finalize")

    res = client.get("/api/dispatches?customer_name=Combo&status=finalized")
    data = res.get_json()
    assert data["total"] == 1
    assert data["results"][0]["dispatch_number"] == "COMBO-FINAL"


def test_pagination_metadata_correct_with_customer_search(client, setup):
    pid = setup["product"]["id"]
    cust = client.post("/api/admin/customers", json={
        "name": "Paginated Customer", "sales_category_id": setup["category"]["id"],
    }).get_json()
    for i in range(3):
        _dispatch(client, pid, cust["id"], f"PGN-{i}")

    res = client.get("/api/dispatches?customer_name=Paginated&limit=2&offset=0")
    data = res.get_json()
    assert data["total"] == 3
    assert len(data["results"]) == 2

    res2 = client.get("/api/dispatches?customer_name=Paginated&limit=2&offset=2")
    assert len(res2.get_json()["results"]) == 1


def test_exports_include_full_filtered_result_set_not_just_a_page(client, setup):
    pid = setup["product"]["id"]
    cust = client.post("/api/admin/customers", json={
        "name": "Export Match Customer", "sales_category_id": setup["category"]["id"],
    }).get_json()
    other = client.post("/api/admin/customers", json={
        "name": "Should Not Appear", "sales_category_id": setup["category"]["id"],
    }).get_json()
    for i in range(6):
        _dispatch(client, pid, cust["id"], f"EXP-{i}")
    _dispatch(client, pid, other["id"], "EXP-OTHER")

    res = client.get("/api/dispatches/export.csv?customer_name=Export Match")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert body.count("Export Match Customer") == 6
    assert "Should Not Appear" not in body


def test_exports_respect_customer_search_across_formats(client, setup):
    pid = setup["product"]["id"]
    cust = client.post("/api/admin/customers", json={
        "name": "Format Check Customer", "sales_category_id": setup["category"]["id"],
    }).get_json()
    _dispatch(client, pid, cust["id"], "FMT-1")

    for fmt, mimetype in [
        ("csv", "text/csv"),
        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("pdf", "application/pdf"),
    ]:
        res = client.get(f"/api/dispatches/export.{fmt}?customer_name=Format Check")
        assert res.status_code == 200
        assert res.mimetype == mimetype


# ---------- the new search endpoint doesn't change anyone's write permissions ----------

def test_operator_can_use_customer_search(client, login_as, setup):
    """Operator's existing ability to create dispatches is untouched by this
    correction — only confirming the new search param is readable."""
    login_as("op1", "password123", "operator")
    res = client.get("/api/dispatches?customer_name=anything")
    assert res.status_code == 200


def test_viewer_can_search_but_still_cannot_create_dispatch(client, login_as, setup):
    login_as("view1", "password123", "viewer")
    res = client.get("/api/dispatches?customer_name=anything")
    assert res.status_code == 200
    write = client.post("/api/dispatches", json={
        "dispatch_number": "VIEW-SEARCH", "date": "2026-07-28", "shift": "Day",
        "customer_id": 1, "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert write.status_code == 403


# ---------- history.html sends the search to the backend ----------

def test_history_html_sends_customer_search_param_to_backend():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "static" / "history.html").read_text(encoding="utf-8")
    assert "params.set('customer_name', customerText)" in source
    assert "results.filter(d => (d.customer_name" not in source, "client-side substring filter must be removed"


def test_history_html_debounces_customer_search_input():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "static" / "history.html").read_text(encoding="utf-8")
    assert "customerSearchTimer" in source
    assert "setTimeout(()=>loadDispatchHistory(), 250)" in source


# ---------- unauthenticated access to /history.html ----------

def test_unauthenticated_history_page_redirects_to_login(client):
    res = client.get("/history.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/"


@pytest.mark.parametrize("role", ["super_admin", "manager", "operator", "viewer"])
def test_authenticated_history_page_loads_for_every_role(client, login_as, role):
    login_as(f"hist_{role}", "password123", role)
    res = client.get("/history.html")
    assert res.status_code == 200
