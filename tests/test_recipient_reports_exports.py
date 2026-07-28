import pytest


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    metro = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    corporate = client.post("/api/admin/sales-categories", json={"name": "Corporate Sales"}).get_json()
    product = client.post("/api/admin/products", json={"name": "Compact Corporate Test"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})
    dakar = client.post("/api/admin/customers", json={"name": "Dakar", "sales_category_id": metro["id"]}).get_json()
    shopwise = client.post("/api/admin/customers", json={"name": "Shopwise Retail LTD", "sales_category_id": corporate["id"]}).get_json()
    return {"metro": metro, "corporate": corporate, "product": product, "dakar": dakar, "shopwise": shopwise}


def _finalize(client, product_id, customer_id, category_id, number):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": "2026-07-28", "shift": "Day",
        "customer_id": customer_id, "sales_category_id": category_id,
        "lines": [{"product_id": product_id, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    return d


# ---------- dispatch list/export filters ----------

def test_dispatch_list_filters_by_sales_category(client, setup):
    _finalize(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"], "F-1")
    _finalize(client, setup["product"]["id"], setup["shopwise"]["id"], setup["corporate"]["id"], "F-2")

    res = client.get(f"/api/dispatches?sales_category_id={setup['metro']['id']}").get_json()
    numbers = [d["dispatch_number"] for d in res["results"]]
    assert "F-1" in numbers
    assert "F-2" not in numbers


def test_dispatch_export_includes_sales_category_column(client, setup):
    _finalize(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"], "F-3")
    res = client.get("/api/dispatches/export.csv")
    text = res.data.decode()
    assert "Sales Category" in text
    assert "Metro Sales" in text


def test_dispatch_export_filtered_by_category_excludes_others(client, setup):
    _finalize(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"], "F-4")
    _finalize(client, setup["product"]["id"], setup["shopwise"]["id"], setup["corporate"]["id"], "F-5")

    res = client.get(f"/api/dispatches/export.csv?sales_category_id={setup['metro']['id']}")
    text = res.data.decode()
    assert "F-4" in text
    assert "F-5" not in text


def test_dispatch_export_xlsx_and_pdf_with_category_filter(client, setup):
    _finalize(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"], "F-6")
    for fmt, mimetype in [("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), ("pdf", "application/pdf")]:
        res = client.get(f"/api/dispatches/export.{fmt}?sales_category_id={setup['metro']['id']}")
        assert res.status_code == 200
        assert res.mimetype == mimetype


def test_dispatch_list_customer_filter_resolves_merges(client, setup):
    danka = client.post("/api/admin/customers", json={"name": "Danka2", "sales_category_id": setup["metro"]["id"], "confirm_not_duplicate": True}).get_json()
    dispatch = _finalize(client, setup["product"]["id"], danka["id"], setup["metro"]["id"], "F-7")
    client.post(f"/api/admin/customers/{danka['id']}/merge", json={
        "target_customer_id": setup["dakar"]["id"], "reason": "spelling variant",
    })

    res = client.get(f"/api/dispatches?customer_id={setup['dakar']['id']}").get_json()
    assert any(d["id"] == dispatch["id"] for d in res["results"])


# ---------- issued-detail drill-down filters ----------

def test_issued_detail_filters_by_category(client, setup):
    _finalize(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"], "F-8")
    detail = client.get(
        f"/api/daily-figures/issued-detail?product_id={setup['product']['id']}&date=2026-07-28&shift=Day"
        f"&sales_category_id={setup['metro']['id']}"
    ).get_json()
    assert detail["total_from_dispatches"] == 100

    detail_other = client.get(
        f"/api/daily-figures/issued-detail?product_id={setup['product']['id']}&date=2026-07-28&shift=Day"
        f"&sales_category_id={setup['corporate']['id']}"
    ).get_json()
    assert detail_other["total_from_dispatches"] == 0


def test_issued_detail_filters_by_recipient(client, setup):
    _finalize(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"], "F-9")
    detail = client.get(
        f"/api/daily-figures/issued-detail?product_id={setup['product']['id']}&date=2026-07-28&shift=Day"
        f"&customer_id={setup['dakar']['id']}"
    ).get_json()
    assert detail["total_from_dispatches"] == 100


# ---------- recipient-totals report ----------

def test_recipient_totals_by_category(client, setup):
    _finalize(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"], "F-10")
    _finalize(client, setup["product"]["id"], setup["shopwise"]["id"], setup["corporate"]["id"], "F-11")

    res = client.get("/api/reports/recipient-totals?date_from=2026-07-28&date_to=2026-07-28&group_by=category").get_json()
    by_name = {r["group_name"]: r for r in res}
    assert by_name["Metro Sales"]["total_issued_base_qty"] == 100
    assert by_name["Metro Sales"]["dispatch_count"] == 1
    assert by_name["Corporate Sales"]["total_issued_base_qty"] == 100


def test_recipient_totals_by_recipient_combines_merged_history(client, setup):
    danka = client.post("/api/admin/customers", json={"name": "Danka3", "sales_category_id": setup["metro"]["id"], "confirm_not_duplicate": True}).get_json()
    _finalize(client, setup["product"]["id"], danka["id"], setup["metro"]["id"], "F-12")
    _finalize(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"], "F-13")
    client.post(f"/api/admin/customers/{danka['id']}/merge", json={
        "target_customer_id": setup["dakar"]["id"], "reason": "dup",
    })

    res = client.get("/api/reports/recipient-totals?date_from=2026-07-28&date_to=2026-07-28&group_by=recipient").get_json()
    dakar_row = next(r for r in res if r["group_name"] == "Dakar")
    assert dakar_row["total_issued_base_qty"] == 200  # both dispatches combined under the canonical name
    assert dakar_row["dispatch_count"] == 2


def test_recipient_totals_requires_date_range(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.get("/api/reports/recipient-totals?group_by=category")
    assert res.status_code == 400


def test_recipient_totals_rejects_bad_group_by(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.get("/api/reports/recipient-totals?date_from=2026-07-28&date_to=2026-07-28&group_by=nonsense")
    assert res.status_code == 400


def test_recipient_totals_export_formats(client, setup):
    _finalize(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"], "F-14")
    for fmt, mimetype in [("csv", "text/csv"), ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), ("pdf", "application/pdf")]:
        res = client.get(f"/api/reports/recipient-totals/export.{fmt}?date_from=2026-07-28&date_to=2026-07-28&group_by=category")
        assert res.status_code == 200
        assert res.mimetype == mimetype


def test_recipient_totals_export_is_audited(client, setup, app):
    _finalize(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"], "F-15")
    client.get("/api/reports/recipient-totals/export.csv?date_from=2026-07-28&date_to=2026-07-28&group_by=recipient")
    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="export", entity_type="recipient_totals_report").first()
        assert entry is not None


def test_viewer_can_view_reports_but_not_manage_categories(client, setup, login_as):
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    res = client.get("/api/reports/recipient-totals?date_from=2026-07-28&date_to=2026-07-28&group_by=category")
    assert res.status_code == 200

    forbidden = client.post("/api/admin/sales-categories", json={"name": "New Cat"})
    assert forbidden.status_code == 403
