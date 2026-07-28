import pytest

from webapp.extensions import db


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    category = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    product = client.post("/api/admin/products", json={"name": "Compact Corporate Test"}).get_json()
    rule = client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10}).get_json()
    product["packaging_rule"] = rule
    return {"category": category, "product": product}


def _make_dispatch(client, product_id, customer_id, category_id, number):
    return client.post("/api/dispatches", json={
        "dispatch_number": number, "date": "2026-07-28", "shift": "Day",
        "customer_id": customer_id, "sales_category_id": category_id,
        "lines": [{"product_id": product_id, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()


# ---------- reassignment ----------

def test_reassign_customer_category(client, setup):
    other_category = client.post("/api/admin/sales-categories", json={"name": "Upcountry Sales"}).get_json()
    customer = client.post("/api/admin/customers", json={"name": "Ayub", "sales_category_id": setup["category"]["id"]}).get_json()

    res = client.patch(f"/api/admin/customers/{customer['id']}", json={"sales_category_id": other_category["id"]})
    assert res.status_code == 200
    assert res.get_json()["sales_category_id"] == other_category["id"]


def test_reassign_category_backfills_previously_uncategorized_dispatches(client, setup, app):
    """
    The API no longer allows creating a NEW dispatch with no category, so
    a genuinely historical uncategorized dispatch (the only way this state
    can occur going forward) is simulated here via direct ORM insertion —
    exactly what a real pre-enhancement production row looks like.
    """
    customer = client.post("/api/admin/customers", json={"name": "Fenecansi"}).get_json()  # no category yet

    from webapp.extensions import db
    from webapp.models.dispatch import Dispatch, DispatchLine
    with app.app_context():
        dispatch = Dispatch(
            dispatch_number="BF-1", date="2026-07-28", shift="Day",
            customer_id=customer["id"], status="finalized",
        )
        db.session.add(dispatch)
        db.session.flush()
        db.session.add(DispatchLine(
            dispatch_id=dispatch.id, product_id=setup["product"]["id"],
            cartons=1, packs=0, pieces=0, base_unit_qty=100,
            packaging_rule_id=setup["product"]["packaging_rule"]["id"],
        ))
        db.session.commit()
        dispatch_id = dispatch.id

    before = client.get(f"/api/dispatches/{dispatch_id}").get_json()
    assert before["sales_category_id"] is None

    client.patch(f"/api/admin/customers/{customer['id']}", json={"sales_category_id": setup["category"]["id"]})

    refetched = client.get(f"/api/dispatches/{dispatch_id}").get_json()
    assert refetched["sales_category_id"] == setup["category"]["id"]
    assert refetched["sales_category_name_snapshot"] == "Metro Sales"


def test_backfill_never_overwrites_an_already_categorized_dispatch(client, setup):
    other_category = client.post("/api/admin/sales-categories", json={"name": "Upcountry Sales"}).get_json()
    customer = client.post("/api/admin/customers", json={"name": "Ayub", "sales_category_id": setup["category"]["id"]}).get_json()
    dispatch = _make_dispatch(client, setup["product"]["id"], customer["id"], setup["category"]["id"], "BF-2")

    client.patch(f"/api/admin/customers/{customer['id']}", json={"sales_category_id": other_category["id"]})

    refetched = client.get(f"/api/dispatches/{dispatch['id']}").get_json()
    assert refetched["sales_category_name_snapshot"] == "Metro Sales"  # untouched historical snapshot


# ---------- merging ----------

def test_merge_preserves_dispatch_history(client, setup):
    source = client.post("/api/admin/customers", json={"name": "Danka", "sales_category_id": setup["category"]["id"]}).get_json()
    target = client.post("/api/admin/customers", json={"name": "Dakar", "sales_category_id": setup["category"]["id"], "confirm_not_duplicate": True}).get_json()
    dispatch = _make_dispatch(client, setup["product"]["id"], source["id"], setup["category"]["id"], "MRG-1")

    res = client.post(f"/api/admin/customers/{source['id']}/merge", json={
        "target_customer_id": target["id"], "reason": "same recipient, spelling variant",
    })
    assert res.status_code == 200
    assert res.get_json()["dispatch_count_preserved"] == 1

    # the dispatch still points at the ORIGINAL customer id — never rewritten
    refetched = client.get(f"/api/dispatches/{dispatch['id']}").get_json()
    assert refetched["customer_id"] == source["id"]
    assert refetched["customer_name_snapshot"] == "Danka"  # historical text untouched

    # but filtering by the canonical (target) customer now includes it
    listing = client.get(f"/api/dispatches?customer_id={target['id']}").get_json()
    assert any(d["id"] == dispatch["id"] for d in listing["results"])


def test_merged_customer_is_deactivated_not_deleted(client, setup, app):
    source = client.post("/api/admin/customers", json={"name": "Danka"}).get_json()
    target = client.post("/api/admin/customers", json={"name": "Dakar", "confirm_not_duplicate": True}).get_json()
    client.post(f"/api/admin/customers/{source['id']}/merge", json={"target_customer_id": target["id"], "reason": "dup"})

    from webapp.models.customer import Customer
    with app.app_context():
        row = db.session.get(Customer, source["id"])
        assert row is not None  # never deleted
        assert row.active is False
        assert row.merged_into_id == target["id"]


def test_cannot_merge_customer_into_itself(client, setup):
    customer = client.post("/api/admin/customers", json={"name": "Dakar", "confirm_not_duplicate": True}).get_json()
    res = client.post(f"/api/admin/customers/{customer['id']}/merge", json={"target_customer_id": customer["id"], "reason": "x"})
    assert res.status_code == 400


def test_merge_chain_resolves_to_final_target(client, setup):
    a = client.post("/api/admin/customers", json={"name": "A Variant", "sales_category_id": setup["category"]["id"]}).get_json()
    b = client.post("/api/admin/customers", json={"name": "B Variant", "confirm_not_duplicate": True}).get_json()
    c = client.post("/api/admin/customers", json={"name": "Canonical Name"}).get_json()

    client.post(f"/api/admin/customers/{a['id']}/merge", json={"target_customer_id": b["id"], "reason": "x"})
    client.post(f"/api/admin/customers/{b['id']}/merge", json={"target_customer_id": c["id"], "reason": "y"})

    # both a and b should resolve to c when filtering
    dispatch = _make_dispatch(client, setup["product"]["id"], a["id"], setup["category"]["id"], "MRG-CHAIN")
    results = client.get(f"/api/dispatches?customer_id={c['id']}").get_json()
    assert any(d["id"] == dispatch["id"] for d in results["results"])


def test_merge_requires_manager_or_super_admin(client, setup, login_as):
    source = client.post("/api/admin/customers", json={"name": "Danka"}).get_json()
    target = client.post("/api/admin/customers", json={"name": "Dakar", "confirm_not_duplicate": True}).get_json()

    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = client.post(f"/api/admin/customers/{source['id']}/merge", json={"target_customer_id": target["id"], "reason": "x"})
    assert res.status_code == 403


def test_merge_is_audited(client, setup, app):
    source = client.post("/api/admin/customers", json={"name": "Danka"}).get_json()
    target = client.post("/api/admin/customers", json={"name": "Dakar", "confirm_not_duplicate": True}).get_json()
    client.post(f"/api/admin/customers/{source['id']}/merge", json={"target_customer_id": target["id"], "reason": "dup"})

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="merge", entity_type="customer").first()
        assert entry is not None


# ---------- temporary recipient review ----------

def test_temporary_review_queue_lists_dispatch_count_and_similar(client, setup):
    client.post("/api/dispatches", json={
        "dispatch_number": "TMP-1", "date": "2026-07-28", "shift": "Day",
        "sales_category_id": setup["category"]["id"], "new_customer_name": "Walk-in",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    queue = client.get("/api/admin/customers/temporary-review").get_json()
    entry = next(c for c in queue if c["name"] == "Walk-in")
    assert entry["dispatch_count"] == 1


def test_approve_temporary_marks_permanent(client, setup):
    client.post("/api/dispatches", json={
        "dispatch_number": "TMP-2", "date": "2026-07-28", "shift": "Day",
        "sales_category_id": setup["category"]["id"], "new_customer_name": "Unknown Customer",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    queue = client.get("/api/admin/customers/temporary-review").get_json()
    customer_id = next(c["id"] for c in queue if c["name"] == "Unknown Customer")

    res = client.post(f"/api/admin/customers/{customer_id}/approve-temporary")
    assert res.status_code == 200
    assert res.get_json()["is_temporary"] is False


def test_reject_temporary_deactivates_without_deleting(client, setup, app):
    client.post("/api/dispatches", json={
        "dispatch_number": "TMP-3", "date": "2026-07-28", "shift": "Day",
        "sales_category_id": setup["category"]["id"], "new_customer_name": "Junk Entry",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    queue = client.get("/api/admin/customers/temporary-review").get_json()
    customer_id = next(c["id"] for c in queue if c["name"] == "Junk Entry")

    res = client.post(f"/api/admin/customers/{customer_id}/reject-temporary", json={"reason": "entered by mistake"})
    assert res.status_code == 200

    from webapp.models.customer import Customer
    with app.app_context():
        row = db.session.get(Customer, customer_id)
        assert row is not None
        assert row.active is False
