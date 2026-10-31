from datetime import datetime

from werkzeug.security import generate_password_hash

from webapp import create_app
from webapp.extensions import db
from webapp.models.customer import CATEGORY_SALESPERSON, Customer
from webapp.models.dispatch import STATUS_FINALIZED, Dispatch, DispatchLine
from webapp.models.packaging_rule import PackagingRule
from webapp.models.product import Product
from webapp.models.sales import SalesBaseline, SalesOpeningStockLine, SalesProfile
from webapp.models.user import ROLE_OPERATOR, User
from webapp.services import section_access_service

BUSINESS_DATE = "2026-10-30"
# The paper sheet the browser test must reproduce:
#   Compact:  previous return 50 + stock added 100 = 150, return today 40 -> 110 sold
#   Napkins:  previous return 10 + stock added  20 =  30, return today  5 ->  25 sold
# "Straws" is a seeded catalogue product that carries nothing today, so the
# driver's daily stock list must never show it.
PRODUCTS = {
    "Compact Standard": {"opening_cartons": 50, "loaded_cartons": 100},
    "Napkins Standard": {"opening_cartons": 10, "loaded_cartons": 20},
}


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
    products, rules = {}, {}
    for order, name in enumerate(PRODUCTS, start=1):
        product = Product.query.filter_by(name=name).first()
        if product is None:
            product = Product(name=name, display_order=order, active=True)
            db.session.add(product); db.session.flush()
        rule = (PackagingRule.query.filter_by(product_id=product.id)
                .order_by(PackagingRule.effective_from.desc(), PackagingRule.id.desc()).first())
        if rule is None:
            rule = PackagingRule(product_id=product.id, cartons_to_packs=10, packs_to_pieces=10,
                                 effective_from=datetime(2026, 1, 1), created_by=user.id)
            db.session.add(rule); db.session.flush()
        products[name], rules[name] = product, rule
    profile = SalesProfile.query.filter_by(user_id=user.id).first()
    if profile is None:
        profile = SalesProfile(user_id=user.id, display_name="Browser Metro Sales", recipient_customer_id=recipient.id)
        db.session.add(profile); db.session.flush()
    baseline = SalesBaseline.query.filter_by(sales_profile_id=profile.id, effective_date=BUSINESS_DATE).first()
    if baseline is None:
        baseline = SalesBaseline(sales_profile_id=profile.id, effective_date=BUSINESS_DATE,
                                 opening_cash_ugx=0, opening_mobile_money_ugx=0,
                                 notes="Disposable browser acceptance baseline", created_by=user.id)
        db.session.add(baseline); db.session.flush()
    for name, spec in PRODUCTS.items():
        exists = SalesOpeningStockLine.query.filter_by(baseline_id=baseline.id, product_id=products[name].id).first()
        if exists is None:
            db.session.add(SalesOpeningStockLine(
                baseline_id=baseline.id, product_id=products[name].id,
                base_unit_qty=spec["opening_cartons"] * rules[name].pieces_per_carton,
                packaging_rule_id=rules[name].id,
            ))
    dispatch = Dispatch.query.filter_by(dispatch_number="BROWSER-1030").first()
    if dispatch is None:
        dispatch = Dispatch(dispatch_number="BROWSER-1030", date=BUSINESS_DATE, shift="Day",
                            customer_id=recipient.id, status=STATUS_FINALIZED, created_by=user.id)
        db.session.add(dispatch); db.session.flush()
        for name, spec in PRODUCTS.items():
            if not spec["loaded_cartons"]:
                continue
            db.session.add(DispatchLine(
                dispatch_id=dispatch.id, product_id=products[name].id,
                cartons=spec["loaded_cartons"], packs=0, pieces=0,
                base_unit_qty=spec["loaded_cartons"] * rules[name].pieces_per_carton,
                packaging_rule_id=rules[name].id,
            ))
    db.session.commit()
