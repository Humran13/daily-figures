"""
Spare Parts master-data population cleanup: BR/BRG -> Bearing name
normalization, Model+Size -> Specifications (regression, unchanged from
the prior round), workbook PRICE -> SparePart.selling_price (never
buying_price), and conservative dedup of the generic bearing name. Tested
against a small synthetic workbook, not the real file (kept out of the
repo/automated suite per the original import task's decision).
"""
from decimal import Decimal

import openpyxl
import pytest
from werkzeug.security import generate_password_hash

from webapp.extensions import db
from webapp.models.spare_part import SparePart
from webapp.models.spare_part_import_row import SparePartImportRow
from webapp.models.spare_part_movement import SparePartMovement
from webapp.models.user import ROLE_SUPER_ADMIN, User
from webapp.services import spare_part_import_service as import_svc
from webapp.services import spare_part_service as sp_service


@pytest.fixture
def workbook_path(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["MOTION VENTURES LTD MACHINE SPARES"])
    ws.append(["#", "SPARE PART", "MODEL", "SIZE", "MACHINE", "NAME", "BUILDING", "CONTACTS", "PRICE"])
    ws.append([1, "brg", 6208, "medium", "tp", None, None, None, "80k"])
    ws.append([2, "BRG", 6204, "small", "tp", None, None, None, "35k"])
    ws.append([3, "Brg", 6208, "medium", "tp", None, None, None, "80k"])  # same as row 1 -> true duplicate
    ws.append([4, "Bearing", "UC210 D1", None, None, None, None, None, "150k"])
    ws.append([5, "Bearings", "UC210 D1", None, None, None, None, None, None])  # dup of row 4, no price (keeps row4's)
    ws.append([6, "Linear Brg", "LMF25UU", None, None, None, None, None, "185k"])
    ws.append([7, "V-Belt", "B52", None, None, None, None, None, "150,000shs"])
    ws.append([8, "Fan Belt", "A41", None, None, None, None, None, "25K"])
    ws.append([9, "Timing Belt", "T5", None, None, None, None, None, "5k"])
    ws.append([10, "Gasket Set", "GS1", None, None, None, None, None, "2k"])
    ws.append([11, "Fuse", "10A", None, None, None, None, None, "17k/15k"])  # ambiguous
    ws.append([12, "Bearings Hajj", "Mechanical", "9\"", None, None, None, None, "100,000shs"])  # untouched name

    path = tmp_path / "test_master_data.xlsx"
    wb.save(path)
    return str(path)


def _make_actor(app):
    with app.app_context():
        actor = User(username="cleanup_actor", password_hash=generate_password_hash("x"), role=ROLE_SUPER_ADMIN)
        db.session.add(actor)
        db.session.commit()
        return actor.id


def _execute(app, workbook_path):
    actor_id = _make_actor(app)
    with app.app_context():
        actor = db.session.get(User, actor_id)
        token = import_svc.file_hash(workbook_path)
        result = import_svc.execute_import(workbook_path, actor, token)
        db.session.commit()
        return result


# ---------- name normalization ----------

def test_brg_normalizes_to_bearing(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        assert SparePart.query.filter_by(name="Bearing", model="6208").count() == 1
        assert SparePart.query.filter_by(name="brg").count() == 0
        assert SparePart.query.filter_by(name="BRG").count() == 0


def test_bearing_models_go_to_specifications(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        part = SparePart.query.filter_by(name="Bearing", model="6208").first()
        assert part.specifications == "6208 - medium"


def test_same_bearing_same_spec_deduplicates(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        # Row 1 (brg/6208) and row 3 (Brg/6208) are a true duplicate.
        assert SparePart.query.filter_by(name="Bearing", model="6208", size="medium").count() == 1


def test_different_bearing_models_remain_separate(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        models = {p.model for p in SparePart.query.filter_by(name="Bearing").all()}
        assert "6208" in models
        assert "6204" in models
        assert SparePart.query.filter_by(name="Bearing").count() >= 3  # 6208, 6204, UC210 D1


def test_repeated_generic_names_normalize_consistently(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        # "Bearing"/"Bearings" (rows 4/5) with the same model dedupe to one record.
        assert SparePart.query.filter_by(name="Bearing", model="UC210 D1").count() == 1


def test_linear_bearing_kept_as_distinct_canonical_name(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        assert SparePart.query.filter_by(name="Linear Bearing", model="LMF25UU").count() == 1
        assert SparePart.query.filter_by(name="Bearing", model="LMF25UU").count() == 0


def test_multi_word_bearing_name_left_untouched(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        assert SparePart.query.filter_by(name="Bearings Hajj").count() == 1


def test_non_bearing_names_unaffected(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        assert SparePart.query.filter_by(name="V-Belt", model="B52").count() == 1


# ---------- Model + Size -> Specifications (regression) ----------

def test_model_maps_to_specifications(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        part = SparePart.query.filter_by(name="V-Belt").first()
        assert part.specifications == "B52"


def test_model_and_size_combine_cleanly(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        part = SparePart.query.filter_by(name="Bearings Hajj").first()
        assert part.specifications == "Mechanical - 9\""


# ---------- price -> selling_price ----------

@pytest.mark.parametrize("name,expected", [
    ("Fan Belt", "25000"),
    ("Timing Belt", "5000"),
    ("Gasket Set", "2000"),
])
def test_k_suffix_prices_normalize_to_selling_price(app, workbook_path, name, expected):
    _execute(app, workbook_path)
    with app.app_context():
        part = SparePart.query.filter_by(name=name).first()
        assert part.selling_price == Decimal(expected)


def test_80k_normalizes_to_selling_price(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        part = SparePart.query.filter_by(name="Bearing", model="6208").first()
        assert part.selling_price == Decimal("80000")


def test_comma_shs_price_parses(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        part = SparePart.query.filter_by(name="V-Belt").first()
        assert part.selling_price == Decimal("150000")


def test_ambiguous_price_not_guessed(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        part = SparePart.query.filter_by(name="Fuse").first()
        assert part.selling_price is None


def test_ambiguous_price_reported(app, workbook_path):
    with app.app_context():
        report = import_svc.preview_import(workbook_path)
        assert len(report["ambiguous_pricing"]) == 1
        assert report["ambiguous_pricing"][0]["raw_price_text"] == "17k/15k"


def test_buying_price_always_none_after_import(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        assert all(p.buying_price is None for p in SparePart.query.all())


def test_dedup_row_without_price_keeps_existing_price(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        # Row 4 ("Bearing"/UC210 D1/150k) creates the record; row 5
        # ("Bearings"/UC210 D1, no price) attaches to it and must not
        # blank out the price already set.
        part = SparePart.query.filter_by(name="Bearing", model="UC210 D1").first()
        assert part.selling_price == Decimal("150000")


# ---------- no stock movements ----------

def test_no_stock_movements_created(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        assert SparePartMovement.query.count() == 0


# ---------- idempotency ----------

def test_reimport_is_idempotent_and_never_overwrites_prices(app, workbook_path):
    _execute(app, workbook_path)
    with app.app_context():
        count_after_first = SparePart.query.count()
        import_rows_after_first = SparePartImportRow.query.count()
        bearing = SparePart.query.filter_by(name="Bearing", model="6208").first()
        bearing.selling_price = Decimal("99999")  # simulate a manual price edit after import
        db.session.commit()

    actor_id = _make_actor_reuse(app)
    with app.app_context():
        actor = db.session.get(User, actor_id)
        token = import_svc.file_hash(workbook_path)
        result2 = import_svc.execute_import(workbook_path, actor, token)
        db.session.commit()
        assert SparePart.query.count() == count_after_first
        assert SparePartImportRow.query.count() == import_rows_after_first
        assert len(result2["new_spares"]) == 0
        # The manual price edit survives — selling_price is never
        # overwritten once set (the is-None guard).
        bearing = SparePart.query.filter_by(name="Bearing", model="6208").first()
        assert bearing.selling_price == Decimal("99999")


def _make_actor_reuse(app):
    with app.app_context():
        existing = User.query.filter_by(username="cleanup_actor2").first()
        if existing:
            return existing.id
        actor = User(username="cleanup_actor2", password_hash=generate_password_hash("x"), role=ROLE_SUPER_ADMIN)
        db.session.add(actor)
        db.session.commit()
        return actor.id


# ---------- clean_spare_name unit tests ----------

def test_clean_spare_name_maps_known_aliases():
    assert sp_service.clean_spare_name("brg") == "Bearing"
    assert sp_service.clean_spare_name("BRG") == "Bearing"
    assert sp_service.clean_spare_name(" Brg ") == "Bearing"
    assert sp_service.clean_spare_name("linear brg") == "Linear Bearing"


def test_clean_spare_name_never_touches_multi_word_names():
    assert sp_service.clean_spare_name("Bearings Hajj") == "Bearings Hajj"
    assert sp_service.clean_spare_name("Grooved Bearings") == "Grooved Bearings"


def test_clean_spare_name_never_guesses_spelling():
    assert sp_service.clean_spare_name("Timming Belt") == "Timming Belt"
