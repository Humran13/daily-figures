"""
Planning > Machine Replacement Estimates ("Estimated Working Period") —
inferred strictly from repeat active STOCK_OUT issue dates attributed to
a machine (movement.machine_id), never from SparePart<->Machine "fits"
master data. See webapp/services/spare_part_analytics_service.py's
replacement_estimates().
"""
from decimal import Decimal

from webapp.extensions import db
from webapp.services import spare_part_analytics_service as analytics

from tests.test_spare_parts import _create_spare_part, _get_or_create_department, _login_spare_parts, _stock_out


def _machine(client, name):
    return client.post("/api/spare-parts/machines", json={"name": name}).get_json()


def _in_and_out(app, client, part_id, machine_id, date, quantity="1", stock_in_qty=None):
    if stock_in_qty:
        client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part_id, "quantity": stock_in_qty})
    return _stock_out(app, client, part_id, quantity=quantity, machine_id=machine_id, date=date)


def test_two_issue_dates_create_one_interval(app, client, make_user):
    _login_spare_parts(client, make_user, "repl1", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    machine = _machine(client, "Wrapper 1")
    _in_and_out(app, client, part["id"], machine["id"], "2026-01-01", stock_in_qty="10")
    _in_and_out(app, client, part["id"], machine["id"], "2026-04-01")

    with app.app_context():
        estimates = analytics.replacement_estimates(spare_part_id=part["id"], machine_id=machine["id"])
    assert len(estimates) == 1
    e = estimates[0]
    assert e.issue_event_count == 2
    assert e.interval_count == 1
    assert e.average_interval_days == Decimal("90")
    assert e.evidence_label == "Limited history"


def test_three_dates_create_two_intervals_and_correct_average(app, client, make_user):
    _login_spare_parts(client, make_user, "repl2", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    machine = _machine(client, "Wrapper 2")
    _in_and_out(app, client, part["id"], machine["id"], "2026-01-01", stock_in_qty="20")
    _in_and_out(app, client, part["id"], machine["id"], "2026-04-01")
    _in_and_out(app, client, part["id"], machine["id"], "2026-07-05")

    with app.app_context():
        estimates = analytics.replacement_estimates(spare_part_id=part["id"], machine_id=machine["id"])
    e = estimates[0]
    assert e.interval_count == 2
    assert e.average_interval_days == Decimal("92.5")
    assert e.evidence_label == "Developing history"
    assert e.last_issue_date == "2026-07-05"
    assert e.estimated_next_need_date is not None


def test_multiple_stock_outs_same_business_date_count_as_one_issue_event(app, client, make_user):
    _login_spare_parts(client, make_user, "repl3", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    machine = _machine(client, "Wrapper 3")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    _stock_out(app, client, part["id"], quantity="1", machine_id=machine["id"], date="2026-01-01")
    _stock_out(app, client, part["id"], quantity="1", machine_id=machine["id"], date="2026-01-01")
    _in_and_out(app, client, part["id"], machine["id"], "2026-04-01")

    with app.app_context():
        estimates = analytics.replacement_estimates(spare_part_id=part["id"], machine_id=machine["id"])
    e = estimates[0]
    assert e.issue_event_count == 2  # two distinct dates, not three rows
    assert e.interval_count == 1
    assert e.average_interval_days == Decimal("90")  # never a spurious 0-day gap


def test_same_spare_different_machines_calculated_separately(app, client, make_user):
    _login_spare_parts(client, make_user, "repl4", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    machine_a = _machine(client, "Machine A")
    machine_b = _machine(client, "Machine B")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "20"})
    _stock_out(app, client, part["id"], quantity="1", machine_id=machine_a["id"], date="2026-01-01")
    _stock_out(app, client, part["id"], quantity="1", machine_id=machine_a["id"], date="2026-04-01")
    _stock_out(app, client, part["id"], quantity="1", machine_id=machine_b["id"], date="2026-01-01")
    _stock_out(app, client, part["id"], quantity="1", machine_id=machine_b["id"], date="2026-06-20")

    with app.app_context():
        estimates = {
            e.machine_id: e for e in analytics.replacement_estimates(spare_part_id=part["id"])
        }
    assert estimates[machine_a["id"]].average_interval_days == Decimal("90")
    assert estimates[machine_b["id"]].average_interval_days != Decimal("90")


def test_different_spare_same_machine_calculated_separately(app, client, make_user):
    _login_spare_parts(client, make_user, "repl5", "super_admin")
    part_a = _create_spare_part(client, unit="pcs")
    part_b = _create_spare_part(client, unit="pcs")
    machine = _machine(client, "Shared Machine")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part_a["id"], "quantity": "20"})
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part_b["id"], "quantity": "20"})
    _stock_out(app, client, part_a["id"], quantity="1", machine_id=machine["id"], date="2026-01-01")
    _stock_out(app, client, part_a["id"], quantity="1", machine_id=machine["id"], date="2026-02-01")
    _stock_out(app, client, part_b["id"], quantity="1", machine_id=machine["id"], date="2026-01-01")
    _stock_out(app, client, part_b["id"], quantity="1", machine_id=machine["id"], date="2026-05-01")

    with app.app_context():
        estimates = {
            e.spare_part_id: e for e in analytics.replacement_estimates(machine_id=machine["id"])
        }
    assert estimates[part_a["id"]].average_interval_days == Decimal("31")
    assert estimates[part_b["id"]].average_interval_days != Decimal("31")


def test_voided_stock_out_excluded_from_replacement_estimate(app, client, make_user):
    _login_spare_parts(client, make_user, "repl6", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    machine = _machine(client, "Wrapper 6")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    out1 = _stock_out(app, client, part["id"], quantity="1", machine_id=machine["id"], date="2026-01-01").get_json()
    _stock_out(app, client, part["id"], quantity="1", machine_id=machine["id"], date="2026-04-01")
    client.post(f"/api/spare-parts/movements/{out1['id']}/void", json={"reason": "mistake"})

    with app.app_context():
        estimates = analytics.replacement_estimates(spare_part_id=part["id"], machine_id=machine["id"])
    # Only one remaining valid issue date -> insufficient history, not a false interval
    assert len(estimates) == 1
    assert estimates[0].interval_count == 0
    assert estimates[0].evidence_label == "Insufficient history"


def test_adjustment_out_excluded_from_replacement_estimate(app, client, make_user):
    _login_spare_parts(client, make_user, "repl7", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    machine = _machine(client, "Wrapper 7")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    client.post("/api/spare-parts/movements/adjustment", json={
        "spare_part_id": part["id"], "quantity": "1", "direction": "out", "reason": "correction",
    })
    with app.app_context():
        estimates = analytics.replacement_estimates(spare_part_id=part["id"], machine_id=machine["id"])
    assert estimates == []


def test_stock_count_correction_excluded_from_replacement_estimate(app, client, make_user):
    _login_spare_parts(client, make_user, "repl8", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    machine = _machine(client, "Wrapper 8")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    client.post("/api/spare-parts/movements/stock-count", json={
        "spare_part_id": part["id"], "counted_quantity": "7", "reason": "count",
    })
    with app.app_context():
        estimates = analytics.replacement_estimates(spare_part_id=part["id"], machine_id=machine["id"])
    assert estimates == []


def test_opening_balance_excluded_from_replacement_estimate(app, client, make_user):
    _login_spare_parts(client, make_user, "repl9", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    machine = _machine(client, "Wrapper 9")
    client.post("/api/spare-parts/movements/opening-balance", json={"spare_part_id": part["id"], "quantity": "10"})
    with app.app_context():
        estimates = analytics.replacement_estimates(spare_part_id=part["id"], machine_id=machine["id"])
    assert estimates == []


def test_movement_without_machine_id_excluded(app, client, make_user):
    _login_spare_parts(client, make_user, "repl10", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    _stock_out(app, client, part["id"], quantity="1")  # no machine_id
    with app.app_context():
        estimates = analytics.replacement_estimates(spare_part_id=part["id"])
    assert estimates == []


def test_one_issue_shows_insufficient_history(app, client, make_user):
    _login_spare_parts(client, make_user, "repl11", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    machine = _machine(client, "Wrapper 11")
    _in_and_out(app, client, part["id"], machine["id"], "2026-01-01", stock_in_qty="10")
    with app.app_context():
        estimates = analytics.replacement_estimates(spare_part_id=part["id"], machine_id=machine["id"])
    assert len(estimates) == 1
    assert estimates[0].issue_event_count == 1
    assert estimates[0].interval_count == 0
    assert estimates[0].average_interval_days is None
    assert estimates[0].estimated_next_need_date is None
    assert estimates[0].evidence_label == "Insufficient history"


def test_estimated_next_need_from_last_issue_plus_average_interval(app, client, make_user):
    _login_spare_parts(client, make_user, "repl12", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    machine = _machine(client, "Wrapper 12")
    _in_and_out(app, client, part["id"], machine["id"], "2026-01-01", stock_in_qty="10")
    _in_and_out(app, client, part["id"], machine["id"], "2026-04-01")
    with app.app_context():
        e = analytics.replacement_estimates(spare_part_id=part["id"], machine_id=machine["id"])[0]
    assert e.last_issue_date == "2026-04-01"
    assert e.average_interval_days == Decimal("90")
    assert e.estimated_next_need_date == "2026-06-30"


def test_average_calculation_keeps_decimal_precision(app, client, make_user):
    _login_spare_parts(client, make_user, "repl13", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    machine = _machine(client, "Wrapper 13")
    _in_and_out(app, client, part["id"], machine["id"], "2026-01-01", stock_in_qty="10")
    _in_and_out(app, client, part["id"], machine["id"], "2026-02-01")  # 31 days
    _in_and_out(app, client, part["id"], machine["id"], "2026-03-01")  # 28 days
    with app.app_context():
        e = analytics.replacement_estimates(spare_part_id=part["id"], machine_id=machine["id"])[0]
    assert e.average_interval_days == Decimal("29.5")  # (31+28)/2, not rounded early


def test_existing_consumption_and_dashboard_math_unchanged(app, client, make_user):
    _login_spare_parts(client, make_user, "repl14", "super_admin")
    part = _create_spare_part(client, unit="pcs")
    client.post("/api/spare-parts/movements/stock-in", json={"spare_part_id": part["id"], "quantity": "10"})
    _stock_out(app, client, part["id"], quantity="2")
    with app.app_context():
        total = analytics.bulk_consumption([part["id"]], "2020-01-01", "2030-01-01")[part["id"]]
    assert total == Decimal("2")


# ---------- route ----------

def test_replacement_estimates_route_requires_machine_selection(app, client, make_user):
    _login_spare_parts(client, make_user, "repl_route1", "super_admin")
    res = client.get("/api/spare-parts/planning/replacement-estimates")
    assert res.status_code == 200
    assert res.get_json()["results"] == []
    assert "guidance" in res.get_json()


def test_replacement_estimates_route_returns_rows_for_selected_machine(app, client, make_user):
    _login_spare_parts(client, make_user, "repl_route2", "super_admin")
    part = _create_spare_part(client, name="Bearing", specifications="6203-2RS", unit="pcs")
    machine = _machine(client, "Wrapper Route")
    _in_and_out(app, client, part["id"], machine["id"], "2026-01-01", stock_in_qty="10")
    _in_and_out(app, client, part["id"], machine["id"], "2026-04-01")

    res = client.get(f"/api/spare-parts/planning/replacement-estimates?machine_id={machine['id']}")
    assert res.status_code == 200
    rows = res.get_json()["results"]
    row = next(r for r in rows if r["spare_part_id"] == part["id"])
    assert row["machine_name"] == "Wrapper Route"
    assert row["average_interval_days"] == "90.0"
    for forbidden in ("china_buying_price", "local_buying_price", "selling_price", "profit"):
        assert forbidden not in row


def test_replacement_estimates_route_denied_to_manager_and_operator(app, client, make_user):
    _login_spare_parts(client, make_user, "repl_route3", "manager")
    assert client.get("/api/spare-parts/planning/replacement-estimates").status_code == 403

    other = app.test_client()
    _login_spare_parts(other, make_user, "repl_route4", "operator")
    assert other.get("/api/spare-parts/planning/replacement-estimates").status_code == 403
