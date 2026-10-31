from datetime import datetime

from werkzeug.security import generate_password_hash

from webapp import create_app
from webapp.extensions import db
from webapp.models.customer import CATEGORY_SALESPERSON, Customer
from webapp.models.packaging_rule import PackagingRule
from webapp.models.product import Product
from webapp.models.sales import SalesBaseline, SalesOpeningStockLine, SalesProfile
from webapp.models.user import ROLE_OPERATOR, User
from webapp.services import section_access_service

app = create_app()
with app.app_context():
    user = User.query.filter_by(username="browser-sales").first()
    if user is None:
        user = User(username="browser-sales", password_hash=generate_password_hash("BrowserPass123"), role=ROLE_OPERATOR, active=True)
        db.session.add(user); db.session.flush()
    section_access_service.set_sections_for_user(user, ["sales"], user)
    recipient = Customer.query.filter_by(name="Browser Sales Dispatch").first()
    if recipient is None:
        recipient = Customer(name="Browser Sales Dispatch", category=CATEGORY_SALESPERSON, active=True, created_by=user.id)
        db.session.add(recipient); db.session.flush()
    product = Product.query.filter_by(name="Compact Standard").first()
    if product is None:
        product = Product(name="Compact Standard", display_order=1, active=True)
        db.session.add(product); db.session.flush()
    rule = PackagingRule.query.filter_by(product_id=product.id).order_by(PackagingRule.id.desc()).first()
    if rule is None:
        rule = PackagingRule(product_id=product.id, cartons_to_packs=10, packs_to_pieces=10,
                             effective_from=datetime(2026, 1, 1), created_by=user.id)
        db.session.add(rule); db.session.flush()
    profile = SalesProfile.query.filter_by(user_id=user.id).first()
    if profile is None:
        profile = SalesProfile(user_id=user.id, display_name="Browser Metro Sales", recipient_customer_id=recipient.id)
        db.session.add(profile); db.session.flush()
    baseline = SalesBaseline.query.filter_by(sales_profile_id=profile.id, effective_date="2026-10-30").first()
    if baseline is None:
        baseline = SalesBaseline(sales_profile_id=profile.id, effective_date="2026-10-30",
                                 opening_cash_ugx=0, opening_mobile_money_ugx=0,
                                 notes="Disposable browser acceptance baseline", created_by=user.id)
        db.session.add(baseline); db.session.flush()
    existing_product_ids = {line.product_id for line in baseline.stock_lines}
    for active_product in Product.query.filter_by(active=True).all():
        active_rule = PackagingRule.query.filter_by(product_id=active_product.id).order_by(PackagingRule.effective_from.desc()).first()
        if active_rule is None:
            active_rule = PackagingRule(product_id=active_product.id, cartons_to_packs=10, packs_to_pieces=10,
                                        effective_from=datetime(2026, 1, 1), created_by=user.id)
            db.session.add(active_rule); db.session.flush()
        if active_product.id in existing_product_ids:
            continue
        db.session.add(SalesOpeningStockLine(
            baseline_id=baseline.id, product_id=active_product.id,
            base_unit_qty=50 * active_rule.pieces_per_carton if active_product.id == product.id else 0,
            packaging_rule_id=active_rule.id,
        ))
    db.session.commit()
