"""Normalized Sales Department accounting and vehicle-reconciliation records.

Amounts are whole UGX integers and stock is stored in base units.  Source
transactions are never replaced by cached totals; services derive every balance
from these rows and the authoritative Finished Goods dispatch ledger.
"""
from datetime import datetime, timezone

from webapp.extensions import db


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


DAY_DRAFT = "draft"
DAY_CLOSED = "closed"
DAY_REOPENED = "reopened"
ACTIVE = "active"
VOID = "void"
PAYMENT_METHODS = ("cash", "mobile_money", "bank", "other")
MONEY_ACCOUNTS = ("cash", "mobile_money", "company", "bank", "other")
SALE_UNITS = ("carton", "pack", "piece")


class SalesProfile(db.Model):
    __tablename__ = "sales_profiles"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True, index=True)
    display_name = db.Column(db.String(120), nullable=False)
    sales_category_id = db.Column(db.Integer, db.ForeignKey("sales_categories.id"), nullable=True, index=True)
    recipient_customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)
    vehicle_label = db.Column(db.String(120), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
    user = db.relationship("User")
    sales_category = db.relationship("SalesCategory")
    recipient_customer = db.relationship("Customer", foreign_keys=[recipient_customer_id])


class SalesBaseline(db.Model):
    __tablename__ = "sales_baselines"
    id = db.Column(db.Integer, primary_key=True)
    sales_profile_id = db.Column(db.Integer, db.ForeignKey("sales_profiles.id"), nullable=False, index=True)
    effective_date = db.Column(db.String(10), nullable=False, index=True)
    opening_cash_ugx = db.Column(db.Integer, nullable=False, default=0)
    opening_mobile_money_ugx = db.Column(db.Integer, nullable=False, default=0)
    notes = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
    stock_lines = db.relationship("SalesOpeningStockLine", cascade="all, delete-orphan", back_populates="baseline")
    __table_args__ = (db.UniqueConstraint("sales_profile_id", "effective_date", name="uq_sales_baseline_profile_date"),)


class SalesOpeningStockLine(db.Model):
    __tablename__ = "sales_opening_stock_lines"
    id = db.Column(db.Integer, primary_key=True)
    baseline_id = db.Column(db.Integer, db.ForeignKey("sales_baselines.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    base_unit_qty = db.Column(db.Integer, nullable=False)
    packaging_rule_id = db.Column(db.Integer, db.ForeignKey("packaging_rules.id"), nullable=False)
    baseline = db.relationship("SalesBaseline", back_populates="stock_lines")
    product = db.relationship("Product")
    packaging_rule = db.relationship("PackagingRule")
    __table_args__ = (
        db.UniqueConstraint("baseline_id", "product_id", name="uq_sales_opening_stock_product"),
        db.CheckConstraint("base_unit_qty >= 0", name="ck_sales_opening_stock_nonnegative"),
    )


class SalesDay(db.Model):
    __tablename__ = "sales_days"
    id = db.Column(db.Integer, primary_key=True)
    sales_profile_id = db.Column(db.Integer, db.ForeignKey("sales_profiles.id"), nullable=False, index=True)
    business_date = db.Column(db.String(10), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default=DAY_DRAFT, index=True)
    actual_cash_count_ugx = db.Column(db.Integer, nullable=True)
    actual_mobile_money_ugx = db.Column(db.Integer, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    close_override_reason = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    closed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)
    reopened_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reopened_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
    profile = db.relationship("SalesProfile")
    closing_lines = db.relationship("SalesVehicleClosingLine", cascade="all, delete-orphan", back_populates="sales_day")
    __table_args__ = (
        db.UniqueConstraint("sales_profile_id", "business_date", name="uq_sales_day_profile_date"),
        db.CheckConstraint("actual_cash_count_ugx IS NULL OR actual_cash_count_ugx >= 0", name="ck_sales_day_actual_cash"),
        db.CheckConstraint("actual_mobile_money_ugx IS NULL OR actual_mobile_money_ugx >= 0", name="ck_sales_day_actual_mobile"),
    )


class SalesVehicleClosingLine(db.Model):
    __tablename__ = "sales_vehicle_closing_lines"
    id = db.Column(db.Integer, primary_key=True)
    sales_day_id = db.Column(db.Integer, db.ForeignKey("sales_days.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    base_unit_qty = db.Column(db.Integer, nullable=False)
    packaging_rule_id = db.Column(db.Integer, db.ForeignKey("packaging_rules.id"), nullable=False)
    corrected_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    correction_reason = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
    sales_day = db.relationship("SalesDay", back_populates="closing_lines")
    product = db.relationship("Product")
    packaging_rule = db.relationship("PackagingRule")
    __table_args__ = (
        db.UniqueConstraint("sales_day_id", "product_id", name="uq_sales_closing_day_product"),
        db.CheckConstraint("base_unit_qty >= 0", name="ck_sales_closing_nonnegative"),
    )


class SalesStockAdjustment(db.Model):
    __tablename__ = "sales_stock_adjustments"
    id = db.Column(db.Integer, primary_key=True)
    sales_profile_id = db.Column(db.Integer, db.ForeignKey("sales_profiles.id"), nullable=False, index=True)
    business_date = db.Column(db.String(10), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    base_unit_delta = db.Column(db.Integer, nullable=False)
    packaging_rule_id = db.Column(db.Integer, db.ForeignKey("packaging_rules.id"), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default=ACTIVE, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    voided_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    voided_at = db.Column(db.DateTime, nullable=True)
    void_reason = db.Column(db.Text, nullable=True)
    __table_args__ = (db.CheckConstraint("base_unit_delta != 0", name="ck_sales_stock_adjustment_nonzero"),)


class SalesInvoice(db.Model):
    __tablename__ = "sales_invoices"
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(40), nullable=False, unique=True, index=True)
    sales_profile_id = db.Column(db.Integer, db.ForeignKey("sales_profiles.id"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)
    business_date = db.Column(db.String(10), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default=ACTIVE, index=True)
    is_opening_receivable = db.Column(db.Boolean, nullable=False, default=False, index=True)
    notes = db.Column(db.Text, nullable=True)
    idempotency_key = db.Column(db.String(80), nullable=False, unique=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    voided_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    voided_at = db.Column(db.DateTime, nullable=True)
    void_reason = db.Column(db.Text, nullable=True)
    profile = db.relationship("SalesProfile")
    customer = db.relationship("Customer")
    lines = db.relationship("SalesInvoiceLine", cascade="all, delete-orphan", back_populates="invoice")


class SalesInvoiceLine(db.Model):
    __tablename__ = "sales_invoice_lines"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("sales_invoices.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True, index=True)
    quantity = db.Column(db.Integer, nullable=False)
    sale_unit = db.Column(db.String(20), nullable=False)
    base_unit_qty = db.Column(db.Integer, nullable=True)
    packaging_rule_id = db.Column(db.Integer, db.ForeignKey("packaging_rules.id"), nullable=True)
    unit_price_ugx = db.Column(db.Integer, nullable=False)
    line_total_ugx = db.Column(db.Integer, nullable=False)
    invoice = db.relationship("SalesInvoice", back_populates="lines")
    product = db.relationship("Product")
    packaging_rule = db.relationship("PackagingRule")
    __table_args__ = (
        db.CheckConstraint("quantity > 0", name="ck_sales_invoice_line_quantity"),
        db.CheckConstraint("unit_price_ugx >= 0", name="ck_sales_invoice_line_price"),
        db.CheckConstraint("line_total_ugx >= 0", name="ck_sales_invoice_line_total"),
    )


class SalesPayment(db.Model):
    __tablename__ = "sales_payments"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("sales_invoices.id"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)
    sales_profile_id = db.Column(db.Integer, db.ForeignKey("sales_profiles.id"), nullable=False, index=True)
    payment_date = db.Column(db.String(10), nullable=False, index=True)
    amount_ugx = db.Column(db.Integer, nullable=False)
    payment_method = db.Column(db.String(20), nullable=False, index=True)
    reference = db.Column(db.String(80), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default=ACTIVE, index=True)
    idempotency_key = db.Column(db.String(80), nullable=False, unique=True)
    entered_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    voided_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    voided_at = db.Column(db.DateTime, nullable=True)
    void_reason = db.Column(db.Text, nullable=True)
    invoice = db.relationship("SalesInvoice")
    __table_args__ = (db.CheckConstraint("amount_ugx > 0", name="ck_sales_payment_positive"),)


class SalesExpense(db.Model):
    __tablename__ = "sales_expenses"
    id = db.Column(db.Integer, primary_key=True)
    sales_profile_id = db.Column(db.Integer, db.ForeignKey("sales_profiles.id"), nullable=False, index=True)
    business_date = db.Column(db.String(10), nullable=False, index=True)
    description = db.Column(db.String(160), nullable=False)
    amount_ugx = db.Column(db.Integer, nullable=False)
    payment_method = db.Column(db.String(20), nullable=False, index=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default=ACTIVE, index=True)
    idempotency_key = db.Column(db.String(80), nullable=False, unique=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    voided_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    voided_at = db.Column(db.DateTime, nullable=True)
    void_reason = db.Column(db.Text, nullable=True)
    __table_args__ = (db.CheckConstraint("amount_ugx > 0", name="ck_sales_expense_positive"),)


class SalesMoneyTransfer(db.Model):
    __tablename__ = "sales_money_transfers"
    id = db.Column(db.Integer, primary_key=True)
    sales_profile_id = db.Column(db.Integer, db.ForeignKey("sales_profiles.id"), nullable=False, index=True)
    business_date = db.Column(db.String(10), nullable=False, index=True)
    from_account = db.Column(db.String(20), nullable=False, index=True)
    to_account = db.Column(db.String(20), nullable=False, index=True)
    amount_ugx = db.Column(db.Integer, nullable=False)
    reference = db.Column(db.String(80), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default=ACTIVE, index=True)
    idempotency_key = db.Column(db.String(80), nullable=False, unique=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    voided_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    voided_at = db.Column(db.DateTime, nullable=True)
    void_reason = db.Column(db.Text, nullable=True)
    __table_args__ = (
        db.CheckConstraint("amount_ugx > 0", name="ck_sales_transfer_positive"),
        db.CheckConstraint("from_account != to_account", name="ck_sales_transfer_distinct"),
    )


class SalesCashAdjustment(db.Model):
    __tablename__ = "sales_cash_adjustments"
    id = db.Column(db.Integer, primary_key=True)
    sales_profile_id = db.Column(db.Integer, db.ForeignKey("sales_profiles.id"), nullable=False, index=True)
    business_date = db.Column(db.String(10), nullable=False, index=True)
    account = db.Column(db.String(20), nullable=False, default="cash")
    amount_delta_ugx = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    __table_args__ = (db.CheckConstraint("amount_delta_ugx != 0", name="ck_sales_cash_adjustment_nonzero"),)
