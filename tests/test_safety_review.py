"""
Regression tests for the pre-deployment correctness/safety review:
required category on every new dispatch, atomic recipient/category updates
on draft dispatches, strengthened (normalized, all-status) import duplicate
detection, and the deployment backup script's fail-safe behavior.
"""
import pathlib
import subprocess

import pytest

from webapp.extensions import db
from webapp.models.customer import Customer, normalize_name

_BACKUP_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "backup_db.sh"


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    metro = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    corporate = client.post("/api/admin/sales-categories", json={"name": "Corporate Sales"}).get_json()
    product = client.post("/api/admin/products", json={"name": "Compact Corporate Test"}).get_json()
    rule = client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10}).get_json()
    product["packaging_rule"] = rule
    dakar = client.post("/api/admin/customers", json={"name": "Dakar", "sales_category_id": metro["id"]}).get_json()
    shopwise = client.post("/api/admin/customers", json={"name": "Shopwise Retail LTD", "sales_category_id": corporate["id"]}).get_json()
    return {"metro": metro, "corporate": corporate, "product": product, "dakar": dakar, "shopwise": shopwise}


def _draft(client, product_id, customer_id, category_id, number="DRAFT-1"):
    return client.post("/api/dispatches", json={
        "dispatch_number": number, "date": "2026-07-28", "shift": "Day",
        "customer_id": customer_id, "sales_category_id": category_id,
        "lines": [{"product_id": product_id, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()


# ---------- 1. every NEW dispatch must have a category ----------

def test_new_dispatch_with_temporary_customer_and_no_category_rejected(client, setup):
    res = client.post("/api/dispatches", json={
        "dispatch_number": "NOCAT-TEMP", "date": "2026-07-28", "shift": "Day",
        "new_customer_name": "Some Walk-in",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 400


def test_duplicate_dispatch_of_uncategorized_historical_dispatch_requires_category(client, setup, app):
    """Duplicating creates a NEW dispatch, so it's bound by the same rule —
    a historical uncategorized source can't produce another null-category row."""
    from webapp.models.dispatch import Dispatch, DispatchLine

    with app.app_context():
        source = Dispatch(
            dispatch_number="HIST-1", date="2026-01-01", shift="Day",
            customer_id=setup["dakar"]["id"], status="finalized",
        )
        # detach dakar from its category for this test, to simulate a
        # customer that (at the time) had none either
        customer = db.session.get(Customer, setup["dakar"]["id"])
        customer.sales_category_id = None
        db.session.add(source)
        db.session.flush()
        db.session.add(DispatchLine(
            dispatch_id=source.id, product_id=setup["product"]["id"],
            cartons=1, packs=0, pieces=0, base_unit_qty=100,
            packaging_rule_id=setup["product"]["packaging_rule"]["id"],
        ))
        db.session.commit()
        source_id = source.id

    res = client.post(f"/api/dispatches/{source_id}/duplicate", json={"dispatch_number": "HIST-1-COPY"})
    assert res.status_code == 400

    # but an explicit override category succeeds
    res2 = client.post(f"/api/dispatches/{source_id}/duplicate", json={
        "dispatch_number": "HIST-1-COPY", "sales_category_id": setup["metro"]["id"],
    })
    assert res2.status_code == 201
    assert res2.get_json()["sales_category_id"] == setup["metro"]["id"]


# ---------- 2 & wrong-category rejection. draft recipient editing ----------

def test_editing_draft_recipient_updates_all_four_fields_atomically(client, setup):
    dispatch = _draft(client, setup["product"]["id"], setup["shopwise"]["id"], setup["corporate"]["id"])

    res = client.patch(f"/api/dispatches/{dispatch['id']}", json={
        "customer_id": setup["dakar"]["id"], "sales_category_id": setup["metro"]["id"],
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["customer_id"] == setup["dakar"]["id"]
    assert data["sales_category_id"] == setup["metro"]["id"]
    assert data["customer_name_snapshot"] == "Dakar"
    assert data["sales_category_name_snapshot"] == "Metro Sales"


def test_editing_draft_recipient_to_wrong_category_pair_rejected(client, setup):
    dispatch = _draft(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"])

    res = client.patch(f"/api/dispatches/{dispatch['id']}", json={
        "customer_id": setup["shopwise"]["id"], "sales_category_id": setup["metro"]["id"],
    })
    assert res.status_code == 400

    # the dispatch must be untouched — no partial update
    unchanged = client.get(f"/api/dispatches/{dispatch['id']}").get_json()
    assert unchanged["customer_id"] == setup["dakar"]["id"]
    assert unchanged["sales_category_id"] == setup["metro"]["id"]
    assert unchanged["customer_name_snapshot"] == "Dakar"
    assert unchanged["sales_category_name_snapshot"] == "Metro Sales"


def test_editing_draft_recipient_to_new_temporary_customer(client, setup):
    dispatch = _draft(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"])

    res = client.patch(f"/api/dispatches/{dispatch['id']}", json={
        "new_customer_name": "Walk-in Replacement", "sales_category_id": setup["metro"]["id"],
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["customer_name_snapshot"] == "Walk-in Replacement"
    assert data["sales_category_id"] == setup["metro"]["id"]


def test_editing_recipient_on_finalized_dispatch_rejected(client, setup):
    """Blocked at the header-edit-lock check (409) before recipient logic
    even runs — reopening first is required, same as any other field."""
    dispatch = _draft(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"])
    client.post(f"/api/dispatches/{dispatch['id']}/finalize")

    res = client.patch(f"/api/dispatches/{dispatch['id']}", json={
        "customer_id": setup["shopwise"]["id"], "sales_category_id": setup["corporate"]["id"],
    })
    assert res.status_code == 409

    unchanged = client.get(f"/api/dispatches/{dispatch['id']}").get_json()
    assert unchanged["customer_id"] == setup["dakar"]["id"]


def test_editing_other_fields_without_touching_recipient_still_works(client, setup):
    dispatch = _draft(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"])
    res = client.patch(f"/api/dispatches/{dispatch['id']}", json={"notes": "just a note"})
    assert res.status_code == 200
    assert res.get_json()["notes"] == "just a note"
    assert res.get_json()["customer_id"] == setup["dakar"]["id"]  # unaffected


def test_recipient_change_is_audited_with_before_and_after(client, setup, app):
    dispatch = _draft(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"])
    client.patch(f"/api/dispatches/{dispatch['id']}", json={
        "customer_id": setup["shopwise"]["id"], "sales_category_id": setup["corporate"]["id"],
    })

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="update", entity_type="dispatch", entity_id=str(dispatch["id"])).first()
        assert entry is not None
        payload = entry.to_dict()
        assert payload["before"]["customer_name_snapshot"] == "Dakar"
        assert payload["after"]["customer_name_snapshot"] == "Shopwise Retail LTD"


# ---------- 3. import duplicate detection: case/whitespace/inactive/temporary/merged ----------

def test_import_skips_case_only_duplicate(client, setup, app):
    from webapp.services.recipient_import_service import execute_batch
    with app.app_context():
        user = _first_user()
        execute_batch(["shopwise retail ltd"], "Corporate Sales", user)  # different case than setup's "Shopwise Retail LTD"
        assert Customer.query.filter(db.func.lower(Customer.name) == "shopwise retail ltd").count() == 1


def test_import_skips_whitespace_only_duplicate(client, setup, app):
    from webapp.services.recipient_import_service import execute_batch
    with app.app_context():
        user = _first_user()
        result = execute_batch(["  Shopwise   Retail   LTD  "], "Corporate Sales", user)
        assert result["created_count"] == 0
        assert result["skipped_count"] == 1


def test_import_skips_inactive_duplicate(client, setup, app):
    client.patch(f"/api/admin/customers/{setup['shopwise']['id']}", json={"active": False})
    from webapp.services.recipient_import_service import execute_batch
    with app.app_context():
        user = _first_user()
        result = execute_batch(["Shopwise Retail LTD"], "Corporate Sales", user)
        assert result["created_count"] == 0
        assert Customer.query.filter_by(name="Shopwise Retail LTD").count() == 1


def test_import_skips_temporary_duplicate(client, setup, app):
    client.post("/api/dispatches", json={
        "dispatch_number": "TEMP-DUP", "date": "2026-07-28", "shift": "Day",
        "sales_category_id": setup["corporate"]["id"], "new_customer_name": "Some Temp Recipient",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    from webapp.services.recipient_import_service import execute_batch
    with app.app_context():
        user = _first_user()
        result = execute_batch(["Some Temp Recipient"], "Corporate Sales", user)
        assert result["created_count"] == 0
        assert Customer.query.filter_by(name="Some Temp Recipient").count() == 1


def test_import_skips_merged_away_duplicate(client, setup, app):
    other = client.post("/api/admin/customers", json={
        "name": "Shopwise Retail LTD Duplicate", "sales_category_id": setup["corporate"]["id"],
        "confirm_not_duplicate": True,
    }).get_json()
    client.post(f"/api/admin/customers/{other['id']}/merge", json={
        "target_customer_id": setup["shopwise"]["id"], "reason": "test",
    })
    from webapp.services.recipient_import_service import execute_batch
    with app.app_context():
        user = _first_user()
        result = execute_batch(["Shopwise Retail LTD Duplicate"], "Corporate Sales", user)
        assert result["created_count"] == 0  # the merged-away name still blocks recreation


def test_import_preview_reports_normalized_duplicate_metadata(client, setup):
    res = client.get("/api/admin/recipient-import/corporate-sales/preview").get_json()
    skipped = next((d for d in res["exact_duplicates_skipped"] if d["name"] == "Shopwise Retail LTD"), None)
    assert skipped is not None
    assert skipped["existing_customer_id"] == setup["shopwise"]["id"]
    assert skipped["existing_active"] is True


def test_repeated_import_creates_no_duplicates_end_to_end(client, setup, app):
    first = client.post("/api/admin/recipient-import/corporate-sales/execute", json={"confirm": True}).get_json()
    second = client.post("/api/admin/recipient-import/corporate-sales/execute", json={"confirm": True}).get_json()
    assert second["created_count"] == 0
    with app.app_context():
        assert Customer.query.filter_by(name="Shopwise Retail LTD").count() == 1


def test_normalized_name_preserves_original_display_spelling(client, setup, app):
    from webapp.services.recipient_import_service import execute_batch
    with app.app_context():
        user = _first_user()
        result = execute_batch(["  Kenjoy   Supermrket  Nansana  "], "Corporate Sales", user)
        assert result["created_count"] == 1
        created = Customer.query.filter(Customer.normalized_name == normalize_name("Kenjoy Supermrket Nansana")).first()
        assert created.name == "  Kenjoy   Supermrket  Nansana  "  # exact spelling preserved, untrimmed


def _first_user():
    from webapp.models.user import User
    return User.query.first()


# ---------- backup script fail-safe behavior ----------

def test_backup_script_fails_and_creates_nothing_when_target_blocked(tmp_path):
    db_file = tmp_path / "fake.db"
    db_file.write_text("not a real db, just needs to exist")
    blocked_backups_dir = tmp_path / "backups"
    blocked_backups_dir.write_text("a file, not a directory")  # sabotages mkdir -p

    result = subprocess.run(
        ["sh", str(_BACKUP_SCRIPT), str(db_file)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "FATAL" in result.stderr or "FATAL" in result.stdout


def test_backup_script_succeeds_and_never_overwrites(tmp_path):
    db_file = tmp_path / "real.db"
    db_file.write_text("db contents")

    first = subprocess.run(["sh", str(_BACKUP_SCRIPT), str(db_file)], capture_output=True, text=True)
    assert first.returncode == 0
    backups_dir = tmp_path / "backups"
    first_backups = list(backups_dir.glob("*.db"))
    assert len(first_backups) == 1
    original_content = first_backups[0].read_text()

    # run again immediately (same-second collision is likely) — must never
    # overwrite the first backup's content
    db_file.write_text("MODIFIED contents")
    second = subprocess.run(["sh", str(_BACKUP_SCRIPT), str(db_file)], capture_output=True, text=True)
    assert second.returncode == 0
    all_backups = list(backups_dir.glob("*.db"))
    assert len(all_backups) == 2
    assert first_backups[0].read_text() == original_content  # untouched by the second run


def test_backup_script_skips_cleanly_when_no_database_exists(tmp_path):
    missing = tmp_path / "does_not_exist.db"
    result = subprocess.run(["sh", str(_BACKUP_SCRIPT), str(missing)], capture_output=True, text=True)
    assert result.returncode == 0
    assert not (tmp_path / "backups").exists()


# ---------- SECRET_KEY enforcement ----------

def test_app_refuses_to_start_without_secret_key(monkeypatch, tmp_path):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from webapp import create_app
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app()


# ---------- historical dispatches remain unchanged ----------

def test_unrelated_dispatch_untouched_when_another_dispatchs_recipient_changes(client, setup):
    d1 = _draft(client, setup["product"]["id"], setup["dakar"]["id"], setup["metro"]["id"], "HIST-A")
    d2 = _draft(client, setup["product"]["id"], setup["shopwise"]["id"], setup["corporate"]["id"], "HIST-B")

    client.patch(f"/api/dispatches/{d1['id']}", json={
        "customer_id": setup["shopwise"]["id"], "sales_category_id": setup["corporate"]["id"],
    })

    untouched = client.get(f"/api/dispatches/{d2['id']}").get_json()
    assert untouched["customer_id"] == setup["shopwise"]["id"]
    assert untouched["sales_category_id"] == setup["corporate"]["id"]
    assert untouched["dispatch_number"] == "HIST-B"
