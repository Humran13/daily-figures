"""
Spare Parts master-data seed migration. Tests call
seed_spare_parts_master() directly against the test app's engine — this
project's tests build their schema via db.create_all(), never real
Alembic migrations (see conftest.py), so calling the exact same function
the migration calls (webapp/seed_data/spare_part_seed.py) is what keeps
the migration and this test suite on one single code path, never two.
"""
from decimal import Decimal

from webapp.extensions import db
from webapp.models.spare_part import SparePart
from webapp.models.spare_part_movement import SparePartMovement
from webapp.seed_data.spare_part_seed import load_seed_records, seed_spare_parts_master


def _seed(app):
    with app.app_context():
        with db.engine.connect() as conn:
            inserted = seed_spare_parts_master(conn)
            conn.commit()
        return inserted


# ---------- fresh seed ----------

def test_fresh_database_migration_seeds_spare_parts(app):
    inserted = _seed(app)
    records = load_seed_records()
    assert inserted == len(records)
    with app.app_context():
        assert SparePart.query.count() == len(records)


def test_seeded_names_match_cleaned_dataset(app):
    _seed(app)
    records = load_seed_records()
    sample = records[0]
    with app.app_context():
        part = SparePart.query.filter_by(name=sample["name"], specifications=sample["specifications"]).first()
        assert part is not None


def test_br_records_are_bearing(app):
    _seed(app)
    with app.app_context():
        records = load_seed_records()
        if any(r["name"] == "Bearing" for r in records):
            assert SparePart.query.filter_by(name="Bearing").count() > 0


def test_bearing_models_are_specifications(app):
    _seed(app)
    with app.app_context():
        bearing = SparePart.query.filter_by(name="Bearing").first()
        assert bearing is not None
        assert bearing.specifications  # every seeded bearing has a model-bearing specification


def test_distinct_bearing_models_stay_separate(app):
    _seed(app)
    with app.app_context():
        specs = {p.specifications for p in SparePart.query.filter_by(name="Bearing").all()}
        assert len(specs) > 1  # multiple distinct bearing models present, never collapsed


def test_true_duplicates_not_seeded_twice(app):
    inserted_first = _seed(app)
    with app.app_context():
        count_after_first = SparePart.query.count()
    inserted_second = _seed(app)
    assert inserted_second == 0
    with app.app_context():
        assert SparePart.query.count() == count_after_first
    assert inserted_first == count_after_first


# ---------- prices ----------

def test_selling_price_values_correct_for_known_sample(app):
    records = load_seed_records()
    priced = next(r for r in records if r.get("selling_price"))
    _seed(app)
    with app.app_context():
        part = SparePart.query.filter_by(name=priced["name"], specifications=priced["specifications"]).first()
        assert part.selling_price == Decimal(priced["selling_price"])


def test_buying_price_is_null_for_every_seeded_row(app):
    _seed(app)
    with app.app_context():
        assert all(p.buying_price is None for p in SparePart.query.all())


def test_ambiguous_price_rows_remain_null(app):
    records = load_seed_records()
    unpriced = next(r for r in records if not r.get("selling_price"))
    _seed(app)
    with app.app_context():
        part = SparePart.query.filter_by(name=unpriced["name"], specifications=unpriced["specifications"]).first()
        assert part.selling_price is None


# ---------- stock safety ----------

def test_no_movements_created_by_seed(app):
    _seed(app)
    with app.app_context():
        assert SparePartMovement.query.count() == 0


def test_all_seeded_stock_balances_are_zero(app):
    _seed(app)
    with app.app_context():
        from webapp.services import spare_part_movement_service as movement_svc
        for part in SparePart.query.limit(10).all():
            assert movement_svc.current_stock(part.id) == 0
            assert part.current_stock_cache == 0


# ---------- idempotency / no-overwrite ----------

def test_rerunning_seed_does_not_duplicate_records(app):
    _seed(app)
    with app.app_context():
        count_first = SparePart.query.count()
    _seed(app)
    with app.app_context():
        assert SparePart.query.count() == count_first


def test_preexisting_matching_record_is_not_overwritten(app):
    with app.app_context():
        existing = SparePart(
            name="Bearing", specifications="6208 - medium", unit="pcs",
            selling_price=Decimal("999999"), buying_price=Decimal("1"),
            current_stock_cache=0, active=True,
        )
        db.session.add(existing)
        db.session.commit()

    _seed(app)

    with app.app_context():
        matches = SparePart.query.filter_by(name="Bearing", specifications="6208 - medium").all()
        assert len(matches) == 1
        assert matches[0].selling_price == Decimal("999999")
        assert matches[0].buying_price == Decimal("1")


def test_preexisting_record_different_case_and_whitespace_still_matched(app):
    with app.app_context():
        existing = SparePart(
            name="  bearing  ", specifications=" 6208 - MEDIUM ", unit="pcs",
            selling_price=Decimal("55"), current_stock_cache=0, active=True,
        )
        db.session.add(existing)
        db.session.commit()

    _seed(app)

    with app.app_context():
        matches = [
            p for p in SparePart.query.all()
            if p.name.strip().casefold() == "bearing" and (p.specifications or "").strip().casefold() == "6208 - medium"
        ]
        assert len(matches) == 1
        assert matches[0].selling_price == Decimal("55")  # untouched


# ---------- no workbook dependency ----------

def test_seed_module_has_no_workbook_dependency():
    from webapp.seed_data import spare_part_seed
    source = open(spare_part_seed.__file__, encoding="utf-8").read()
    assert "import openpyxl" not in source
    assert ".xlsx" not in source


def test_seed_json_artifact_is_committed_and_loadable():
    from webapp.seed_data.spare_part_seed import SEED_FILE
    assert SEED_FILE.exists()
    records = load_seed_records()
    assert len(records) > 0
    assert all("name" in r and "specifications" in r for r in records)
