from pathlib import Path

import pytest


HISTORY_HTML = (Path(__file__).resolve().parent.parent / "static" / "history.html").read_text(encoding="utf-8")
INDEX_HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
APP_SHELL_JS = (Path(__file__).resolve().parent.parent / "static" / "app-shell.js").read_text(encoding="utf-8")
FILTER_AUTOCOMPLETE_JS = (Path(__file__).resolve().parent.parent / "static" / "filter-autocomplete.js").read_text(encoding="utf-8")


@pytest.fixture
def reporting_setup(client, login_as):
    login_as("report_root", "password123", "super_admin")

    products = []
    for name in ("Compact Standard", "Compact Corporate", "Napkins Standard"):
        product = client.post("/api/admin/products", json={"name": name}).get_json()
        client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
            "cartons_to_packs": 10, "packs_to_pieces": 10,
        })
        products.append(product)

    metro = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    corporate = client.post("/api/admin/sales-categories", json={"name": "Corporate"}).get_json()
    dakar = client.post("/api/admin/customers", json={
        "name": "Dakar", "sales_category_id": metro["id"],
    }).get_json()
    derrick = client.post("/api/admin/customers", json={
        "name": "Derrick", "sales_category_id": corporate["id"],
    }).get_json()
    return {
        "compact": products[0], "compact_corporate": products[1], "napkins": products[2],
        "metro": metro, "corporate": corporate, "dakar": dakar, "derrick": derrick,
    }


def _dispatch(client, number, date, customer_id, lines):
    record = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date, "shift": "Day",
        "customer_id": customer_id, "lines": lines,
    }).get_json()
    assert client.post(f"/api/dispatches/{record['id']}/finalize").status_code == 200
    return record


def _line(product_id):
    return {"product_id": product_id, "cartons": 1, "packs": 0, "pieces": 0}


def test_filter_option_endpoints_search_database_after_one_character(client, reporting_setup):
    customers = client.get("/api/reports/filter-options/customers?q=D").get_json()
    assert {row["name"] for row in customers} >= {"Dakar", "Derrick"}

    products = client.get("/api/reports/filter-options/products?q=C").get_json()
    assert {row["name"] for row in products} >= {"Compact Standard", "Compact Corporate"}
    assert all(row["active"] for row in products)


def test_history_uses_one_reusable_id_backed_autocomplete_everywhere():
    assert HISTORY_HTML.count("createFilterAutocomplete({inputId:") == 6
    for input_id in ("fCustomer", "rReturnedBy", "fProduct", "rProduct", "pProduct", "hProduct"):
        assert f"inputId:'{input_id}'" in HISTORY_HTML
    assert '<script src="/filter-autocomplete.js"></script>' in HISTORY_HTML
    assert '<script src="/filter-autocomplete.js"></script>' in INDEX_HTML
    assert "input.dataset.selectedId = String(item.id)" in FILTER_AUTOCOMPLETE_JS
    assert "input.dataset.selectedId = '';" in FILTER_AUTOCOMPLETE_JS
    assert "query.length < 1" in FILTER_AUTOCOMPLETE_JS
    assert "ArrowDown" in FILTER_AUTOCOMPLETE_JS and "ArrowUp" in FILTER_AUTOCOMPLETE_JS and "Enter" in FILTER_AUTOCOMPLETE_JS


def test_main_daily_figures_history_uses_explicit_product_selection_too():
    assert "historyProductAutocomplete = FilterAutocomplete.create" in INDEX_HTML
    assert "historyProductAutocomplete.selectedId()" in INDEX_HTML
    assert "products.find(p=>p.name.toLowerCase().includes(productText))" not in INDEX_HTML


@pytest.mark.parametrize("query", [
    "customer_id={customer}",
    "product_id={product}",
    "customer_id={customer}&product_id={product}",
    "date_from=2026-09-01&date_to=2026-09-30&customer_id={customer}&product_id={product}",
])
def test_dispatch_screen_api_combines_exact_filters(client, reporting_setup, query):
    s = reporting_setup
    wanted = _dispatch(client, "SEPT-WANTED", "2026-09-12", s["dakar"]["id"], [
        _line(s["compact"]["id"]), _line(s["napkins"]["id"]),
    ])
    _dispatch(client, "SEPT-OTHER-CUSTOMER", "2026-09-12", s["derrick"]["id"], [_line(s["compact"]["id"])])
    _dispatch(client, "AUG-WANTED", "2026-08-31", s["dakar"]["id"], [_line(s["compact"]["id"])])

    url = query.format(customer=s["dakar"]["id"], product=s["compact"]["id"])
    rows = client.get(f"/api/dispatches?limit=200&{url}").get_json()["results"]
    ids = {row["id"] for row in rows}
    assert wanted["id"] in ids
    if "customer_id=" in query:
        assert all(row["customer_id"] == s["dakar"]["id"] for row in rows)
    if "date_from=" in query:
        assert all("2026-09-01" <= row["date"] <= "2026-09-30" for row in rows)


def test_dispatch_export_filters_lines_as_well_as_parent_records(client, reporting_setup):
    s = reporting_setup
    _dispatch(client, "DAKAR-MIXED", "2026-09-12", s["dakar"]["id"], [
        _line(s["compact"]["id"]), _line(s["napkins"]["id"]),
    ])
    response = client.get(
        f"/api/dispatches/export.csv?date_from=2026-09-01&date_to=2026-09-30"
        f"&customer_id={s['dakar']['id']}&product_id={s['compact']['id']}"
    )
    text = response.data.decode("utf-8")
    assert response.status_code == 200
    assert "DAKAR-MIXED" in text
    assert "Compact Standard" in text
    assert "Napkins Standard" not in text


@pytest.mark.parametrize("fmt", ["csv", "xlsx", "pdf"])
def test_every_dispatch_export_format_receives_only_selected_product_lines(
    client, reporting_setup, monkeypatch, fmt,
):
    s = reporting_setup
    _dispatch(client, "DAKAR-ALL-FORMATS", "2026-09-12", s["dakar"]["id"], [
        _line(s["compact"]["id"]), _line(s["napkins"]["id"]),
    ])
    captured = {}

    def capture_export(_fmt, **kwargs):
        captured["format"] = _fmt
        captured["rows"] = kwargs["rows"]
        return b"verified-export"

    monkeypatch.setattr("webapp.routes.dispatches.build_export", capture_export)
    response = client.get(
        f"/api/dispatches/export.{fmt}?customer_id={s['dakar']['id']}"
        f"&product_id={s['compact']['id']}"
    )

    assert response.status_code == 200
    assert captured["format"] == fmt
    assert {row["product_name"] for row in captured["rows"]} == {"Compact Standard"}


def test_monthly_sales_category_and_individual_customer_exports(client, reporting_setup):
    s = reporting_setup
    _dispatch(client, "METRO-SEPT", "2026-09-05", s["dakar"]["id"], [_line(s["compact"]["id"])])
    _dispatch(client, "CORP-SEPT", "2026-09-05", s["derrick"]["id"], [_line(s["compact"]["id"])])
    _dispatch(client, "METRO-AUG", "2026-08-31", s["dakar"]["id"], [_line(s["compact"]["id"])])

    base = "date_from=2026-09-01&date_to=2026-09-30"
    category_csv = client.get(
        f"/api/dispatches/export.csv?{base}&sales_category_id={s['metro']['id']}"
    ).data.decode("utf-8")
    assert "METRO-SEPT" in category_csv
    assert "CORP-SEPT" not in category_csv
    assert "METRO-AUG" not in category_csv

    customer_csv = client.get(
        f"/api/dispatches/export.csv?{base}&customer_id={s['dakar']['id']}"
    ).data.decode("utf-8")
    assert "METRO-SEPT" in customer_csv
    assert "CORP-SEPT" not in customer_csv
    assert "METRO-AUG" not in customer_csv


def test_returns_and_production_exports_do_not_leak_sibling_products(client, reporting_setup):
    s = reporting_setup
    lines = [_line(s["compact"]["id"]), _line(s["napkins"]["id"])]

    returned = client.post("/api/returns", json={
        "date": "2026-09-08", "customer_id": s["dakar"]["id"], "lines": lines,
    }).get_json()
    client.post(f"/api/returns/{returned['id']}/finalize")
    returns_csv = client.get(
        f"/api/returns/export.csv?product_id={s['compact']['id']}"
    ).data.decode("utf-8")
    assert "Compact Standard" in returns_csv
    assert "Napkins Standard" not in returns_csv

    produced = client.post("/api/production", json={
        "date": "2026-09-08", "shift": "Day", "lines": lines,
    }).get_json()
    client.post(f"/api/production/{produced['id']}/finalize")
    production_csv = client.get(
        f"/api/production/export.csv?product_id={s['compact']['id']}"
    ).data.decode("utf-8")
    assert "Compact Standard" in production_csv
    assert "Napkins Standard" not in production_csv


def test_daily_figures_month_and_exact_product_match_screen_and_export(client, reporting_setup):
    s = reporting_setup
    for product, date in ((s["compact"], "2026-09-02"), (s["napkins"], "2026-09-03"), (s["compact"], "2026-08-31")):
        response = client.post("/api/daily-figures", json={
            "product_id": product["id"], "date": date, "shift": "Day",
            "opening": {"cartons": 1, "packs": 0, "pieces": 0},
        })
        assert response.status_code in (200, 201)

    query = f"date_from=2026-09-01&date_to=2026-09-30&product_id={s['compact']['id']}"
    rows = client.get(f"/api/daily-figures/history?{query}").get_json()
    assert [(row["date"], row["product_name"]) for row in rows] == [("2026-09-02", "Compact Standard")]

    exported = client.get(f"/api/daily-figures/export.csv?{query}").data.decode("utf-8")
    assert "2026-09-02" in exported and "Compact Standard" in exported
    assert "Napkins Standard" not in exported
    assert "2026-08-31" not in exported


def test_arbitrary_month_controls_feed_the_same_screen_and_export_params():
    for field_id in ("fMonth", "rMonth", "pMonth", "hMonth"):
        assert f'id="{field_id}" type="month"' in HISTORY_HTML
        assert f"applyMonthFilter(params, '{field_id}')" in HISTORY_HTML
    assert "monthBounds" in HISTORY_HTML
    assert 'id="hMonth" type="month"' in INDEX_HTML
    assert "historyMonthBounds" in INDEX_HTML
    assert "'/api/dispatches/export.'+fmt+'?'+params.toString()" in HISTORY_HTML
    assert "'/api/daily-figures/export.'+fmt+'?'+params.toString()" in HISTORY_HTML


def test_no_dead_end_login_message_and_shared_shell_redirects_401():
    static_dir = Path(__file__).resolve().parent.parent / "static"
    for path in static_dir.glob("*.html"):
        assert "Sign in from the main app first." not in path.read_text(encoding="utf-8")
    assert "response.status === 401 && !onLoginPage" in APP_SHELL_JS
    assert "location.replace('/')" in APP_SHELL_JS


def test_stale_logout_cannot_invalidate_newest_session(app, client, login_as):
    login_as("session_owner", "password123", "manager")
    newest = app.test_client()
    assert newest.post("/api/login", json={
        "username": "session_owner", "password": "password123",
    }).status_code == 200

    # The stale cookie clears only itself; it never increments the database
    # version and therefore cannot invalidate the newer client.
    assert client.post("/api/logout").status_code == 200
    assert newest.get("/api/session").get_json()["authed"] is True
