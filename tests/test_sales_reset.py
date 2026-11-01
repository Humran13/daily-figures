"""Sales transactional reset: proves what is cleared and what survives.

The reset exists because testing the Sales workflow leaves rows that afterwards
read like real accounting history.  These tests build a database that has both
Sales transactions and the master/other-department data around them, run the
reset, and then assert:

  * every Sales transaction table is empty;
  * products, packaging rules, categories, customers, users, sales profiles,
    permissions, settings, the audit trail and the Finished Goods loads that
    feed Given Stock are all still there, row for row;
  * opening the Sales day afterwards shows a genuinely clean day;
  * the CLI refuses to delete without confirmation, backs the database up
    first, and is a no-op dry run by default.
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from webapp.extensions import db

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "reset_sales_data.py"
SALES_TABLES = (
    "sales_debt_payments", "sales_debts", "sales_debtors", "sales_payments",
    "sales_invoice_lines", "sales_invoices", "sales_expenses", "sales_money_transfers",
    "sales_cash_adjustments", "sales_stock_adjustments", "sales_vehicle_closing_lines",
    "sales_days", "sales_opening_stock_lines", "sales_baselines",
)


def _login(client, user):
    from webapp.models.section import SECTION_SALES
    from webapp.services import section_access_service

    section_access_service.ensure_seeded()
    section_access_service.set_sections_for_user(user, [SECTION_SALES], user)
    db.session.commit()
    response = client.post("/api/login",
                           json={"username": user.username, "password": "password123",
                                 "section": SECTION_SALES})
    assert response.status_code == 200, response.get_json()


def _seed(app):
    """One salesperson with a fully worked day, plus the master data around it."""
    from werkzeug.security import generate_password_hash

    from webapp.models.customer import CATEGORY_SALESPERSON, Customer
    from webapp.models.dispatch import STATUS_FINALIZED, Dispatch, DispatchLine
    from webapp.models.packaging_rule import PackagingRule
    from webapp.models.product import Product
    from webapp.models.sales import (
        DAY_CLOSED, SalesBaseline, SalesCashAdjustment, SalesDay, SalesDebt, SalesDebtPayment,
        SalesDebtor, SalesExpense, SalesInvoice, SalesInvoiceLine, SalesMoneyTransfer,
        SalesOpeningStockLine, SalesPayment, SalesProfile, SalesStockAdjustment,
        SalesVehicleClosingLine,
    )
    from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, User

    def make_user(name, role):
        row = User(username=name, password_hash=generate_password_hash("password123"),
                   role=role, active=True)
        db.session.add(row)
        db.session.flush()
        return row

    manager = make_user("reset-manager", ROLE_MANAGER)
    operator = make_user("reset-salesperson", ROLE_OPERATOR)
    recipient = Customer(name="Reset Dispatch", category=CATEGORY_SALESPERSON, active=True)
    product = Product(name="Reset Compact", display_order=1, active=True)
    db.session.add_all([recipient, product])
    db.session.flush()
    rule = PackagingRule(product_id=product.id, cartons_to_packs=10, packs_to_pieces=10,
                         effective_from=datetime(2026, 1, 1), created_by=manager.id)
    db.session.add(rule)
    db.session.flush()
    profile = SalesProfile(user_id=operator.id, display_name="Reset Salesperson",
                           recipient_customer_id=recipient.id)
    dispatch = Dispatch(dispatch_number="D-RESET", date="2026-10-30", shift="Day",
                        customer_id=recipient.id, status=STATUS_FINALIZED, created_by=manager.id)
    db.session.add_all([profile, dispatch])
    db.session.flush()
    db.session.add(DispatchLine(dispatch_id=dispatch.id, product_id=product.id, cartons=100, packs=0,
                                pieces=0, base_unit_qty=10000, packaging_rule_id=rule.id))
    baseline = SalesBaseline(sales_profile_id=profile.id, effective_date="2026-10-30",
                             opening_cash_ugx=0, opening_mobile_money_ugx=0, created_by=manager.id)
    db.session.add(baseline)
    db.session.flush()
    db.session.add(SalesOpeningStockLine(baseline_id=baseline.id, product_id=product.id,
                                         base_unit_qty=5000, packaging_rule_id=rule.id))
    day = SalesDay(sales_profile_id=profile.id, business_date="2026-10-30", status=DAY_CLOSED,
                   actual_cash_count_ugx=1_000, mobile_money_from_sales_ugx=0, created_by=operator.id,
                   closed_by=operator.id, closed_at=datetime(2026, 10, 30, 18, 0))
    db.session.add(day)
    db.session.flush()
    db.session.add(SalesVehicleClosingLine(sales_day_id=day.id, product_id=product.id,
                                           base_unit_qty=4000, packaging_rule_id=rule.id))
    invoice = SalesInvoice(reference="SAL-RESET01", sales_profile_id=profile.id,
                           business_date="2026-10-30", idempotency_key=f"reset-inv-{uuid4().hex}",
                           created_by=operator.id)
    db.session.add(invoice)
    db.session.flush()
    db.session.add(SalesInvoiceLine(invoice_id=invoice.id, product_id=product.id, quantity=10,
                                    sale_unit="carton", base_unit_qty=1000,
                                    packaging_rule_id=rule.id, unit_price_ugx=79000,
                                    line_total_ugx=790000))
    db.session.add_all([
        SalesPayment(invoice_id=invoice.id, sales_profile_id=profile.id, payment_date="2026-10-30",
                     amount_ugx=790000, payment_method="cash",
                     idempotency_key=f"reset-pay-{uuid4().hex}", entered_by=operator.id),
        SalesExpense(sales_profile_id=profile.id, business_date="2026-10-30", description="Lunch",
                     amount_ugx=10000, payment_method="cash",
                     idempotency_key=f"reset-exp-{uuid4().hex}", created_by=operator.id),
        SalesMoneyTransfer(sales_profile_id=profile.id, business_date="2026-10-30",
                           from_account="cash", to_account="mobile_money", amount_ugx=5000,
                           idempotency_key=f"reset-tr-{uuid4().hex}", created_by=operator.id),
        SalesCashAdjustment(sales_profile_id=profile.id, business_date="2026-10-30", account="cash",
                            amount_delta_ugx=-1000, reason="Test adjustment", created_by=manager.id),
        SalesStockAdjustment(sales_profile_id=profile.id, business_date="2026-10-30",
                             product_id=product.id, base_unit_delta=-100,
                             packaging_rule_id=rule.id, reason="Test adjustment",
                             created_by=manager.id),
    ])
    debtor = SalesDebtor(sales_profile_id=profile.id, name="Darius", name_key="darius",
                         created_by=operator.id)
    db.session.add(debtor)
    db.session.flush()
    db.session.add(SalesDebt(debtor_id=debtor.id, sales_profile_id=profile.id,
                             business_date="2026-10-30", amount_ugx=50000,
                             idempotency_key=f"reset-debt-{uuid4().hex}", created_by=operator.id))
    db.session.flush()
    db.session.add(SalesDebtPayment(debtor_id=debtor.id, sales_profile_id=profile.id,
                                    payment_date="2026-10-30", amount_ugx=20000,
                                    payment_method="mobile_money",
                                    idempotency_key=f"reset-debtpay-{uuid4().hex}",
                                    entered_by=operator.id))
    db.session.commit()
    return dict(operator=operator, manager=manager, profile=profile, product=product,
                recipient=recipient, day=day)


def test_reset_clears_sales_transactions_and_keeps_master_data(app):
    from webapp.services import sales_reset_service

    _seed(app)
    before = sales_reset_service.sales_row_counts()
    master_before = sales_reset_service.preserved_row_counts()
    assert all(count > 0 for count in before.values()), before

    result = sales_reset_service.reset_sales_transactions()
    db.session.commit()

    after = sales_reset_service.sales_row_counts()
    assert all(count == 0 for count in after.values()), after
    assert result["deleted"]["sales_days"] == 1
    assert result["deleted"]["sales_invoice_lines"] == 1

    # Master data and every other department must be untouched, row for row.
    master_after = sales_reset_service.preserved_row_counts()
    assert master_after == master_before
    assert master_after["products"] == 1
    assert master_after["packaging_rules"] == 1
    assert master_after["sales_profiles"] == 1
    assert master_after["customers"] == 1
    assert master_after["users"] == 2
    assert master_after["dispatches"] == 1 and master_after["dispatch_lines"] == 1

    verified = sales_reset_service.verify_sales_reset()
    assert verified["empty"] is True and verified["remaining"] == {}


def test_a_reset_sales_day_opens_clean(app, client):
    """The point of the reset: no old date, product or figure comes back."""
    from webapp.services import sales_reset_service

    s = _seed(app)
    sales_reset_service.reset_sales_transactions()
    db.session.commit()

    _login(client, s["operator"])
    summary = client.get(f"/api/sales/days/2026-10-30?sales_profile_id={s['profile'].id}")
    assert summary.status_code == 200, summary.get_json()
    body = summary.get_json()
    assert body["day"] is None
    assert body["stock_warnings"] == []
    assert body["invoices"] == [] and body["expenses"] == []
    assert body["debts"] == [] and body["debt_payments"] == []
    assert body["totals"]["total_sales_ugx"] == 0
    assert body["totals"]["total_expenses_ugx"] == 0
    assert body["totals"]["new_debt_ugx"] == 0
    assert body["totals"]["cash_at_hand_ugx"] is None
    # The one row still listed is the Finished Goods load, which is not Sales
    # data and is deliberately preserved.  Nothing the reset removed survives:
    # no counted return, no current-day opening, no sold figure, no sale.
    assert len(body["stock"]) == 1
    row = body["stock"][0]
    assert row["product_name"] == "Reset Compact"
    assert row["closing"] is None and row["return_counted"] is False
    assert row["derived_sold"] is None
    assert row["recorded_sold"]["base_unit_qty"] == 0
    assert row["opening"] is None
    assert row["dispatch_loaded"]["cartons"] == 100
    assert row["can_set_opening"] is True
    assert row["reconciled"] is False
    # The catalogue is still there, so the day can be worked again from zero.
    options = client.get(f"/api/sales/days/2026-10-30/vehicle-stock/options?sales_profile_id={s['profile'].id}")
    assert options.status_code == 200
    assert [p["name"] for p in options.get_json()["products"]] == ["Reset Compact"]


def _run_cli(*args, db_path):
    env = {**os.environ, "DB_PATH": str(db_path), "SECRET_KEY": "test-secret",
           "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run([sys.executable, str(SCRIPT), *args, "--database", str(db_path)],
                          capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))


def _sales_counts_from_file(db_path):
    import sqlite3

    with sqlite3.connect(str(db_path)) as conn:
        return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in SALES_TABLES}


def test_cli_refuses_to_delete_without_confirmation_and_is_a_dry_run(app):
    _seed(app)
    db_path = Path(os.environ["DB_PATH"])
    db.session.remove()
    db.engine.dispose()

    dry = _run_cli(db_path=db_path)
    assert dry.returncode == 0, dry.stderr
    assert "DRY RUN" in dry.stdout and "nothing was deleted" in dry.stdout
    assert all(count > 0 for count in _sales_counts_from_file(db_path).values())

    refused = _run_cli("--apply", db_path=db_path)
    assert refused.returncode == 2
    assert "RESET-SALES-DATA" in refused.stderr
    assert all(count > 0 for count in _sales_counts_from_file(db_path).values())


def test_cli_apply_backs_up_first_then_empties_the_sales_tables(app):
    _seed(app)
    db_path = Path(os.environ["DB_PATH"])
    db.session.remove()
    db.engine.dispose()

    applied = _run_cli("--apply", "--confirm", "RESET-SALES-DATA", db_path=db_path)
    assert applied.returncode == 0, applied.stderr + applied.stdout
    assert "Sales tables now empty: yes" in applied.stdout
    assert all(count == 0 for count in _sales_counts_from_file(db_path).values())

    backups = sorted(db_path.parent.glob(db_path.name + ".bak-*"))
    assert backups, "the reset must leave a verified backup next to the database"
    import sqlite3

    with sqlite3.connect(str(backups[-1])) as conn:
        # The backup is the state *before* the reset: the day and the invoice
        # are still in it.
        assert conn.execute("SELECT COUNT(*) FROM sales_days").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sales_invoice_lines").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
