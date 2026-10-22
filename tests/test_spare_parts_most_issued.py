"""
Dashboard "Most Taken Out Spare Parts" ranking — genuine active STOCK_OUT
movements only, ranked by total quantity issued over a selectable trailing
period (business_date, not created_at). See
webapp/services/spare_part_movement_service.most_issued_spare_parts.
"""
from decimal import Decimal

from webapp.extensions import db
from webapp.services import spare_part_movement_service as movement_svc

from tests.test_spare_parts import _create_spare_part, _get_or_create_department, _login_spare_parts, _stock_out


def _stock_in(client, spare_part_id, quantity):
    return client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": spare_part_id, "quantity": quantity})


def test_stock_out_contributes_to_ranking(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_mi1", "manager")
    part = _create_spare_part(client, unit="pcs")
    _stock_in(client, part["id"], "10")
    res = _stock_out(app, client, part["id"], quantity="4")
    assert res.status_code == 201

    results = movement_svc.most_issued_spare_parts(days=None)
    row = next(r for r in results if r["spare_part_id"] == part["id"])
    assert Decimal(row["total_quantity"]) == Decimal("4")
    assert row["issue_count"] == 1


def test_stock_in_does_not_count(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_mi2", "manager")
    part = _create_spare_part(client, unit="pcs")
    _stock_in(client, part["id"], "10")
    results = movement_svc.most_issued_spare_parts(days=None)
    assert not any(r["spare_part_id"] == part["id"] for r in results)


def test_adjustment_out_does_not_count(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_mi3", "manager")
    part = _create_spare_part(client, unit="pcs")
    _stock_in(client, part["id"], "10")
    client.post("/api/spare-parts/movements/adjustment", json={
        "spare_part_id": part["id"], "quantity": "3", "direction": "out", "reason": "correction",
    })
    results = movement_svc.most_issued_spare_parts(days=None)
    assert not any(r["spare_part_id"] == part["id"] for r in results)


def test_voided_stock_out_does_not_count(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_mi4", "manager")
    part = _create_spare_part(client, unit="pcs")
    _stock_in(client, part["id"], "10")
    out = _stock_out(app, client, part["id"], quantity="5").get_json()
    client.post(f"/api/spare-parts/movements/{out['id']}/void", json={"reason": "mistake"})
    results = movement_svc.most_issued_spare_parts(days=None)
    assert not any(r["spare_part_id"] == part["id"] for r in results)


def test_quantities_aggregate_across_multiple_stock_outs(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_mi5", "manager")
    part = _create_spare_part(client, unit="pcs")
    _stock_in(client, part["id"], "20")
    _stock_out(app, client, part["id"], quantity="3")
    _stock_out(app, client, part["id"], quantity="2")
    results = movement_svc.most_issued_spare_parts(days=None)
    row = next(r for r in results if r["spare_part_id"] == part["id"])
    assert Decimal(row["total_quantity"]) == Decimal("5")
    assert row["issue_count"] == 2


def test_ranking_order_by_total_quantity_descending(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_mi6", "manager")
    big = _create_spare_part(client, unit="pcs")
    small = _create_spare_part(client, unit="pcs")
    _stock_in(client, big["id"], "100")
    _stock_in(client, small["id"], "100")
    _stock_out(app, client, big["id"], quantity="40")
    _stock_out(app, client, small["id"], quantity="5")
    results = movement_svc.most_issued_spare_parts(days=None, limit=50)
    ids = [r["spare_part_id"] for r in results]
    assert ids.index(big["id"]) < ids.index(small["id"])


def test_top_limit_is_respected(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_mi7", "manager")
    for _ in range(7):
        part = _create_spare_part(client, unit="pcs")
        _stock_in(client, part["id"], "10")
        _stock_out(app, client, part["id"], quantity="1")
    results = movement_svc.most_issued_spare_parts(days=None, limit=5)
    assert len(results) == 5


def test_name_specification_and_unit_are_correct_and_no_pricing_fields(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_mi8", "manager")
    part = _create_spare_part(client, name="Bearing", specifications="6203-2RS", unit="pcs")
    _stock_in(client, part["id"], "10")
    _stock_out(app, client, part["id"], quantity="1")
    results = movement_svc.most_issued_spare_parts(days=None)
    row = next(r for r in results if r["spare_part_id"] == part["id"])
    assert row["name"] == "Bearing"
    assert row["specifications"] == "6203-2RS"
    assert row["unit"] == "pcs"
    for forbidden in ("buying_price", "china_buying_price", "local_buying_price", "selling_price", "profit"):
        assert forbidden not in row


def test_period_filter_uses_business_date(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_mi9", "manager")
    part = _create_spare_part(client, unit="pcs")
    _stock_in(client, part["id"], "10")
    old_out = _stock_out(app, client, part["id"], quantity="2").get_json()
    with app.app_context():
        from webapp.models.spare_part_movement import SparePartMovement
        movement = db.session.get(SparePartMovement, old_out["id"])
        movement.business_date = "2000-01-01"
        db.session.commit()

    results_30 = movement_svc.most_issued_spare_parts(days=30)
    assert not any(r["spare_part_id"] == part["id"] for r in results_30)
    results_all = movement_svc.most_issued_spare_parts(days=None)
    assert any(r["spare_part_id"] == part["id"] for r in results_all)


def test_dashboard_existing_counts_unchanged(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_mi10", "manager")
    _create_spare_part(client, unit="pcs")
    summary = movement_svc.dashboard_summary()
    assert "total_active_spare_parts" in summary
    assert "healthy_stock_count" in summary
    assert "low_stock_count" in summary
    assert "no_stock_count" in summary


def test_most_issued_endpoint_empty_state(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_mi11", "manager")
    res = client.get("/api/spare-parts/dashboard/most-issued?days=all")
    assert res.status_code == 200
    assert res.get_json()["results"] == []


def test_most_issued_endpoint_rejects_unknown_period(app, client, make_user):
    _login_spare_parts(client, make_user, "mgr_mi12", "manager")
    res = client.get("/api/spare-parts/dashboard/most-issued?days=42")
    assert res.status_code == 400
