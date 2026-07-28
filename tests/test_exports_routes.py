import pytest


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Compact Corporate Test"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Test Sales Category"}).get_json()
    customer = client.post("/api/admin/customers", json={"name": "Dalca", "sales_category_id": category["id"]}).get_json()
    return {"product": product, "customer": customer, "category": category}


def _finalize_dispatch(client, product_id, customer_id, date, shift, cartons, packs, pieces, number):
    created = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date, "shift": shift, "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    client.post(f"/api/dispatches/{created['id']}/finalize")
    return created


# ---------- dispatch exports ----------

def test_dispatch_export_csv_contains_finalized_line(client, setup):
    _finalize_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-07-28", "Day", 2, 3, 4, "EXP-1")
    res = client.get("/api/dispatches/export.csv")
    assert res.status_code == 200
    assert b"Dispatch Transactions" in res.data
    assert b"EXP-1" in res.data
    assert b"Dalca" in res.data
    assert b"234" in res.data  # total pieces for that line


def test_dispatch_export_respects_status_filter(client, setup):
    draft = client.post("/api/dispatches", json={
        "dispatch_number": "EXP-DRAFT", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    _finalize_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-07-28", "Day", 1, 0, 0, "EXP-FINAL")

    res = client.get("/api/dispatches/export.csv?status=finalized")
    text = res.data.decode()
    assert "EXP-FINAL" in text
    assert "EXP-DRAFT" not in text
    assert "status=finalized" in text  # filters line


def test_dispatch_export_xlsx_and_pdf_work(client, setup):
    _finalize_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-07-28", "Day", 1, 0, 0, "EXP-2")
    xlsx = client.get("/api/dispatches/export.xlsx")
    assert xlsx.status_code == 200
    assert xlsx.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    pdf = client.get("/api/dispatches/export.pdf")
    assert pdf.status_code == 200
    assert pdf.data.startswith(b"%PDF")


def test_dispatch_export_unsupported_format_rejected(client, setup):
    res = client.get("/api/dispatches/export.txt")
    assert res.status_code == 400


def test_dispatch_export_is_audited(client, setup, app):
    _finalize_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-07-28", "Day", 1, 0, 0, "EXP-3")
    client.get("/api/dispatches/export.csv")

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="export", entity_type="dispatch").first()
        assert entry is not None


def test_viewer_can_export_dispatches(client, setup, login_as):
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    res = client.get("/api/dispatches/export.csv")
    assert res.status_code == 200


# ---------- daily figures exports ----------

def test_daily_figures_export_xlsx_contains_product(client, setup):
    client.post("/api/daily-figures", json={
        "product_id": setup["product"]["id"], "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    res = client.get("/api/daily-figures/export.xlsx")
    assert res.status_code == 200
    assert res.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_daily_figures_export_filters_by_product(client, setup):
    other = client.post("/api/admin/products", json={"name": "Lavex Test"}).get_json()
    client.post(f"/api/admin/products/{other['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})

    for pid, name in [(setup["product"]["id"], "Compact Corporate Test"), (other["id"], "Lavex Test")]:
        client.post("/api/daily-figures", json={
            "product_id": pid, "date": "2026-07-28", "shift": "Day",
            "opening": {"cartons": 1, "packs": 0, "pieces": 0},
            "return_": {"cartons": 0, "packs": 0, "pieces": 0},
            "production": {"cartons": 0, "packs": 0, "pieces": 0},
        })

    res = client.get(f"/api/daily-figures/export.csv?product_id={setup['product']['id']}")
    text = res.data.decode()
    assert "Compact Corporate Test" in text
    assert "Lavex Test" not in text


# ---------- date-range summary report ----------

def test_summary_report_aggregates_across_range(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-27", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-28", "Day", 0, 2, 0, "SUM-1")

    res = client.get("/api/reports/summary?date_from=2026-07-27&date_to=2026-07-28")
    assert res.status_code == 200
    rows = res.get_json()
    row = next(r for r in rows if r["product_id"] == pid)
    assert row["opening_base_qty"] == 1000
    assert row["production_base_qty"] == 100
    assert row["issued_base_qty"] == 20
    assert row["closing_base_qty"] == 1000 + 100 - 20


def test_summary_report_requires_date_range(client, setup):
    res = client.get("/api/reports/summary")
    assert res.status_code == 400


def test_summary_report_export_formats(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    for fmt, mimetype in [("csv", "text/csv"), ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), ("pdf", "application/pdf")]:
        res = client.get(f"/api/reports/summary/export.{fmt}?date_from=2026-07-28&date_to=2026-07-28")
        assert res.status_code == 200
        assert res.mimetype == mimetype


def test_summary_report_excludes_untouched_products(client, setup):
    untouched = client.post("/api/admin/products", json={"name": "Never Touched"}).get_json()
    client.post(f"/api/admin/products/{untouched['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})

    res = client.get("/api/reports/summary?date_from=2026-07-01&date_to=2026-07-31")
    rows = res.get_json()
    assert not any(r["product_id"] == untouched["id"] for r in rows)
