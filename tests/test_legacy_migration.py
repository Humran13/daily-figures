import pytest

from webapp.legacy_entries import get_db


def _insert_legacy_row(date, shift, product, opening, return_val, production, issued, closing):
    conn = get_db()
    conn.execute("""
        INSERT INTO entries (date, shift, product, opening, return_val, production, issued, closing, notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '2026-07-28T00:00:00')
    """, (date, shift, product, opening, return_val, production, issued, closing))
    conn.commit()
    row_id = conn.execute("SELECT id FROM entries WHERE date=? AND shift=? AND product=?",
                           (date, shift, product)).fetchone()[0]
    conn.close()
    return row_id


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


@pytest.fixture
def compact_corporate(client, super_admin):
    """Matches the real product name so legacy rows resolve by exact name."""
    product = client.post("/api/admin/products", json={"name": "Compact Corporate"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return product


def test_migrate_decodes_and_creates_daily_figure(client, compact_corporate, app):
    # 1.24 cartons-notation = 1 carton + 2 packs + 4 pieces = 124 pieces (per spec example)
    _insert_legacy_row("2026-07-20", "Day", "Compact Corporate", 1.24, 0.0, 0.10, 0.05, 1.29)

    res = client.post("/api/admin/legacy/migrate")
    assert res.status_code == 200
    summary = res.get_json()
    assert summary["migrated"] == 1
    assert summary["flagged"] == 0

    view = client.get(f"/api/daily-figures/{compact_corporate['id']}?date=2026-07-20&shift=Day").get_json()
    assert view["opening"]["base_qty"] == 124
    assert view["production"]["base_qty"] == 10  # 0.10 decodes to 1 pack, 0 pieces = 10 base units

    from webapp.models.daily_figure import StockAdjustment
    with app.app_context():
        adj = StockAdjustment.query.filter_by(product_id=compact_corporate["id"]).first()
        assert adj is not None
        assert "Migrated legacy issued figure" in adj.reason


def test_migrate_is_idempotent(client, compact_corporate):
    _insert_legacy_row("2026-07-20", "Day", "Compact Corporate", 1.0, 0.0, 0.0, 0.0, 1.0)
    first = client.post("/api/admin/legacy/migrate").get_json()
    assert first["migrated"] == 1

    second = client.post("/api/admin/legacy/migrate").get_json()
    assert second["migrated"] == 0
    assert second["skipped_already_migrated"] == 1


def test_migrate_flags_unresolvable_product_name(client, compact_corporate):
    _insert_legacy_row("2026-07-20", "Day", "Some Unknown Legacy Code", 1.0, 0.0, 0.0, 0.0, 1.0)
    res = client.post("/api/admin/legacy/migrate").get_json()
    assert res["flagged"] == 1
    assert res["migrated"] == 0

    flags = client.get("/api/admin/legacy/flags?resolved=0").get_json()
    assert len(flags) == 4  # opening, return_val, production, issued all flagged
    assert all(f["product_name"] == "Some Unknown Legacy Code" for f in flags)


def test_migrate_flags_out_of_range_value(client, compact_corporate):
    # 0.99 -> packs=9, pieces=9 — both within Compact's 10/10 ratio, so THIS
    # is actually valid; use Napkins-style overflow instead via a real
    # product with a 6-pack ratio.
    napkins = client.post("/api/admin/products", json={"name": "Napkins Corporate Test"}).get_json()
    client.post(f"/api/admin/products/{napkins['id']}/packaging-rules", json={
        "cartons_to_packs": 6, "packs_to_pieces": 10,
    })
    _insert_legacy_row("2026-07-20", "Day", "Napkins Corporate Test", 1.74, 0.0, 0.0, 0.0, 1.74)

    res = client.post("/api/admin/legacy/migrate").get_json()
    assert res["flagged"] == 1

    flags = client.get("/api/admin/legacy/flags?resolved=0").get_json()
    assert any(f["field"] == "opening" and f["product_name"] == "Napkins Corporate Test" for f in flags)


def test_migrate_flags_products_with_no_packaging_rule(client, super_admin):
    unconfigured = client.post("/api/admin/products", json={"name": "Kantimba"}).get_json()
    _insert_legacy_row("2026-07-20", "Day", "Kantimba", 1.0, 0.0, 0.0, 0.0, 1.0)
    res = client.post("/api/admin/legacy/migrate").get_json()
    assert res["flagged"] == 1
    assert res["migrated"] == 0


def test_legacy_shorthand_alias_resolves(client, super_admin):
    product = client.post("/api/admin/products", json={"name": "KingMax"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"carton_to_pieces": 60})
    _insert_legacy_row("2026-07-20", "Day", "K.max", 1.0, 0.0, 0.0, 0.0, 1.0)

    res = client.post("/api/admin/legacy/migrate").get_json()
    assert res["migrated"] == 1

    view = client.get(f"/api/daily-figures/{product['id']}?date=2026-07-20&shift=Day").get_json()
    assert view["opening"]["base_qty"] == 60


def test_migrate_requires_super_admin(client, login_as, compact_corporate):
    client.post("/api/logout")
    login_as("mgr1", "password123", "manager")
    res = client.post("/api/admin/legacy/migrate")
    assert res.status_code == 403


def test_resolve_flag(client, compact_corporate):
    _insert_legacy_row("2026-07-20", "Day", "Nonexistent Product", 1.0, 0.0, 0.0, 0.0, 1.0)
    client.post("/api/admin/legacy/migrate")
    flags = client.get("/api/admin/legacy/flags?resolved=0").get_json()
    flag_id = flags[0]["id"]

    res = client.patch(f"/api/admin/legacy/flags/{flag_id}", json={})
    assert res.status_code == 200
    assert res.get_json()["resolved"] is True

    unresolved = client.get("/api/admin/legacy/flags?resolved=0").get_json()
    assert not any(f["id"] == flag_id for f in unresolved)
