"""Explicit reset of the Sales Department's transactional data.

Testing the Sales workflow leaves rows behind that afterwards look like real
accounting history: sales days, counted vehicle stock, sales cards, expenses,
money movements, debts and starting points.  This service removes exactly that
transaction layer so the department can be exercised again from a genuine zero
state, and removes nothing else.

What is cleared (child rows first, so no foreign key is ever left dangling):

``sales_debt_payments``, ``sales_debts``, ``sales_debtors``,
``sales_payments``, ``sales_invoice_lines``, ``sales_invoices``,
``sales_expenses``, ``sales_money_transfers``, ``sales_cash_adjustments``,
``sales_stock_adjustments``, ``sales_vehicle_closing_lines``, ``sales_days``,
``sales_opening_stock_lines``, ``sales_baselines``.

What is deliberately preserved: products, packaging rules, sales categories,
customers, users, sales profiles (who sells and which dispatch recipient they
collect from), sections and permissions, feature flags, company settings, the
audit trail, every other department's data, the database schema and the whole
Alembic history.  Nothing here drops, truncates or recreates a table, and no
``DELETE`` ever names a master-data table.

Finished Goods dispatches are *not* Sales transactions: they are the evidence
that stock reached the salesperson's vehicle, they belong to the Finished Goods
department, and this service never touches them.  A salesperson whose vehicle
was loaded today will therefore still see that load as Given Stock, which is
correct after a reset - the load really did happen.
"""
from sqlalchemy import text

from webapp.extensions import db

# Child rows before their parents.  Every name here is Sales *transaction*
# data; the tuple is also what the dry run and the verification report print.
SALES_TRANSACTION_TABLES = (
    "sales_debt_payments",
    "sales_debts",
    "sales_debtors",
    "sales_payments",
    "sales_invoice_lines",
    "sales_invoices",
    "sales_expenses",
    "sales_money_transfers",
    "sales_cash_adjustments",
    "sales_stock_adjustments",
    "sales_vehicle_closing_lines",
    "sales_days",
    "sales_opening_stock_lines",
    "sales_baselines",
)

# Printed after a reset so an operator can see, in the same report, that the
# configuration the department depends on is still there.
PRESERVED_TABLES = (
    "products",
    "packaging_rules",
    "sales_categories",
    "customers",
    "users",
    "sales_profiles",
    "sections",
    "user_section_access",
    "feature_flags",
    "company_settings",
    # Other departments, listed so the report proves they are untouched: the
    # Finished Goods loads that feed Given Stock live in these tables.
    "dispatches",
    "dispatch_lines",
    "production_records",
    "production_lines",
    "return_records",
    "return_lines",
    "daily_figures",
    "audit_log",
)


def _existing_tables(names):
    """Only the tables this deployment actually has, in the given order."""
    rows = db.session.execute(
        text("SELECT name FROM sqlite_master WHERE type = 'table'")
    ).scalars().all()
    present = set(rows)
    return [name for name in names if name in present]


def table_counts(names):
    """Row count per table, skipping anything this schema does not have."""
    counts = {}
    for name in _existing_tables(names):
        counts[name] = int(db.session.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar() or 0)
    return counts


def sales_row_counts():
    """What the reset would delete, without deleting anything."""
    return table_counts(SALES_TRANSACTION_TABLES)


def preserved_row_counts():
    """The master data the reset leaves exactly as it is."""
    return table_counts(PRESERVED_TABLES)


def reset_sales_transactions():
    """Delete every Sales transaction row.  The caller owns the transaction.

    Returns ``{"deleted": {table: rows}, "kept": {table: rows}}`` so the caller
    can commit and then report precisely what happened.  Deleting is done with
    plain ``DELETE`` (never ``DROP``), child-first, so a failure part way leaves
    the caller free to roll the whole thing back.
    """
    deleted = {}
    for name in _existing_tables(SALES_TRANSACTION_TABLES):
        result = db.session.execute(text(f"DELETE FROM {name}"))
        deleted[name] = int(result.rowcount if result.rowcount is not None and result.rowcount >= 0 else 0)
    db.session.flush()
    return {"deleted": deleted, "kept": preserved_row_counts()}


def verify_sales_reset():
    """Every Sales transaction table must now be empty; master data must not be."""
    remaining = {name: count for name, count in sales_row_counts().items() if count}
    return {"empty": not remaining, "remaining": remaining, "kept": preserved_row_counts()}
