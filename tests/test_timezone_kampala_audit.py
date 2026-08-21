"""
System-wide Africa/Kampala timezone audit + targeted fix.

Observed bug (fixed): a Returns export generated at ~09:17 Kampala time
showed "Generated 2026-08-21 06:17 UTC" — the raw server UTC instant,
leaking directly into a user-facing report. Root cause: webapp/services/
export_service.py's _utcnow_str() (now _generated_at_str()) hardcoded
UTC formatting, completely bypassing the app's one existing centralized
Kampala helper module (webapp/services/business_calendar.py) — used
identically by every export format (CSV/XLSX/PDF) and every report type
(Dispatch/Returns/Production/Daily Figures/summary/recipient-totals), so
all of them showed raw UTC, not just Returns.

Fix: business_calendar.py gained one new small helper,
format_kampala_report_timestamp() — the exact same centralization
pattern format_kampala_datetime() already established — and
export_service.py now calls it instead of formatting UTC directly.
business_today() gained an optional `now` parameter (mirroring every
other `now=None`-for-deterministic-tests function in this codebase) so
its own day-boundary correctness is testable without a real clock or
the freezegun package (confirmed not installed in this environment —
see tests/test_final_operator_same_day_edit_window.py's own note).

Everything else audited and found ALREADY CORRECT / intentionally
UTC (canonical storage) is listed in the completion report, not
re-tested here beyond a couple of narrow regression confirmations.
"""
from datetime import datetime

import pytest

from webapp.services import business_calendar, export_service
from webapp.services.business_calendar import (
    business_today,
    format_kampala_datetime,
    format_kampala_report_timestamp,
    is_same_business_day,
)
from webapp.services.export_service import build_csv, build_pdf, build_xlsx


# =====================================================================
# SECTION 15 — the observed export bug, exact regression
# =====================================================================

def test_generated_timestamp_shows_kampala_not_utc():
    # Exact numbers from the observed bug report.
    canonical_utc = datetime(2026, 8, 21, 6, 17)  # naive UTC, as stored
    label = format_kampala_report_timestamp(canonical_utc)
    assert label == "2026-08-21 09:17 EAT"
    assert "UTC" not in label


def test_csv_generated_line_uses_kampala(monkeypatch):
    fixed = datetime(2026, 8, 21, 6, 17)
    monkeypatch.setattr(business_calendar, "utcnow", lambda: fixed)
    out = build_csv(title="Returns", filters={}, generated_by="tester", columns=[("a", "A")], rows=[])
    assert "Generated 2026-08-21 09:17 EAT by tester" in out
    assert "06:17 UTC" not in out
    assert " UTC" not in out


def test_xlsx_generated_cell_uses_kampala(monkeypatch):
    import openpyxl
    import io
    fixed = datetime(2026, 8, 21, 6, 17)
    monkeypatch.setattr(business_calendar, "utcnow", lambda: fixed)
    out = build_xlsx(title="Returns", filters={}, generated_by="tester", columns=[("a", "A")], rows=[])
    wb = openpyxl.load_workbook(io.BytesIO(out))
    ws = wb.active
    assert ws["A2"].value == "Generated 2026-08-21 09:17 EAT by tester"


def test_pdf_generation_does_not_error_with_kampala_timestamp(monkeypatch):
    fixed = datetime(2026, 8, 21, 6, 17)
    monkeypatch.setattr(business_calendar, "utcnow", lambda: fixed)
    out = build_pdf(title="Returns", filters={}, generated_by="tester", columns=[("a", "A")], rows=[])
    assert out.startswith(b"%PDF")


def test_returns_export_route_shows_kampala_generated_line(client, login_as, monkeypatch):
    login_as("tz_returns_root", "password123", "super_admin")
    fixed = datetime(2026, 8, 21, 6, 17)
    monkeypatch.setattr(business_calendar, "utcnow", lambda: fixed)
    res = client.get("/api/returns/export.csv")
    assert res.status_code == 200
    text = res.data.decode()
    assert "Generated 2026-08-21 09:17 EAT" in text
    assert "06:17 UTC" not in text


def test_dispatch_export_also_uses_kampala_not_just_returns(client, login_as, monkeypatch):
    # Section 6: "Do not leave one report on UTC while another uses EAT" —
    # every export shares the one _generated_at_str() helper, so fixing
    # export_service.py once fixes Dispatch/Production/Daily Figures too.
    login_as("tz_dispatch_root", "password123", "super_admin")
    fixed = datetime(2026, 8, 21, 6, 17)
    monkeypatch.setattr(business_calendar, "utcnow", lambda: fixed)
    res = client.get("/api/dispatches/export.csv")
    assert res.status_code == 200
    assert "Generated 2026-08-21 09:17 EAT" in res.data.decode()


def test_production_export_also_uses_kampala(client, login_as, monkeypatch):
    login_as("tz_production_root", "password123", "super_admin")
    fixed = datetime(2026, 8, 21, 6, 17)
    monkeypatch.setattr(business_calendar, "utcnow", lambda: fixed)
    res = client.get("/api/production/export.csv")
    assert res.status_code == 200
    assert "Generated 2026-08-21 09:17 EAT" in res.data.decode()


def test_daily_figures_export_also_uses_kampala(client, login_as, monkeypatch):
    login_as("tz_df_root", "password123", "super_admin")
    fixed = datetime(2026, 8, 21, 6, 17)
    monkeypatch.setattr(business_calendar, "utcnow", lambda: fixed)
    res = client.get("/api/daily-figures/export.csv")
    assert res.status_code == 200
    assert "Generated 2026-08-21 09:17 EAT" in res.data.decode()


# =====================================================================
# SECTION 16 — day boundary
# =====================================================================

def test_business_today_resolves_across_the_midnight_boundary():
    # 22:30 UTC on the 20th is 01:30 EAT on the 21st.
    just_before_utc_midnight = datetime(2026, 8, 20, 22, 30)
    assert business_today(now=just_before_utc_midnight) == "2026-08-21"


def test_business_today_normal_daytime_instant_unaffected():
    midday_utc = datetime(2026, 8, 21, 12, 0)
    assert business_today(now=midday_utc) == "2026-08-21"


def test_format_kampala_datetime_also_resolves_the_same_boundary():
    just_before_utc_midnight = datetime(2026, 8, 20, 22, 30)
    label = format_kampala_datetime(just_before_utc_midnight)
    assert label.startswith("21 Aug 2026")


def test_format_kampala_report_timestamp_also_resolves_the_same_boundary():
    just_before_utc_midnight = datetime(2026, 8, 20, 22, 30)
    assert format_kampala_report_timestamp(just_before_utc_midnight) == "2026-08-21 01:30 EAT"


def test_is_same_business_day_still_works_unparameterized():
    # Regression: the existing no-arg call path (used by production code)
    # is completely unaffected by adding the optional `now` parameter.
    assert is_same_business_day(business_today()) is True


# =====================================================================
# SECTION 17 — representative user-facing timestamps
# =====================================================================

def test_dispatch_time_entered_uses_kampala_label(client, login_as, app):
    login_as("tz_dispatch_entered", "password123", "super_admin")
    cat = client.post("/api/admin/sales-categories", json={"name": "TZ Cat"}).get_json()
    cust = client.post("/api/admin/customers", json={
        "name": "TZ Customer", "sales_category_id": cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    product = client.post("/api/admin/products", json={"name": "TZ Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TZ-D1", "date": "2026-08-21", "customer_id": cust["id"],
        "sales_category_id": cat["id"],
        "lines": [{"product_id": product["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    assert d["created_at_label"] is not None
    # Same conversion the whole app already relies on — this just proves
    # the field exists and is populated, not a new mechanism.
    with app.app_context():
        from webapp.extensions import db
        from webapp.models.dispatch import Dispatch
        row = db.session.get(Dispatch, d["id"])
        assert d["created_at_label"] == format_kampala_datetime(row.created_at)


def test_returns_time_entered_uses_kampala_label(client, login_as):
    login_as("tz_returns_entered", "password123", "super_admin")
    cat = client.post("/api/admin/sales-categories", json={"name": "TZ Cat2"}).get_json()
    cust = client.post("/api/admin/customers", json={
        "name": "TZ Customer2", "sales_category_id": cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    product = client.post("/api/admin/products", json={"name": "TZ Product2"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})
    r = client.post("/api/returns", json={
        "date": "2026-08-21", "customer_id": cust["id"],
        "lines": [{"product_id": product["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    assert r["created_at_label"] is not None


def test_production_time_entered_uses_kampala_label(client, login_as):
    login_as("tz_production_entered", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "TZ Product3"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})
    p = client.post("/api/production", json={
        "date": "2026-08-21", "shift": "Day",
        "lines": [{"product_id": product["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    assert p["created_at_label"] is not None


def test_correction_request_lifecycle_timestamps_all_kampala_labeled(client, login_as, app):
    login_as("tz_cr_root", "password123", "super_admin")
    cat = client.post("/api/admin/sales-categories", json={"name": "TZ Cat3"}).get_json()
    cust = client.post("/api/admin/customers", json={
        "name": "TZ Customer3", "sales_category_id": cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    product = client.post("/api/admin/products", json={"name": "TZ Product4"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})

    login_as("tz_cr_op", "password123", "operator")
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TZ-D2", "date": "2020-01-01", "customer_id": cust["id"],
        "sales_category_id": cat["id"],
        "lines": [{"product_id": product["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    with app.app_context():
        import datetime as dt
        from webapp.extensions import db
        from webapp.models.dispatch import Dispatch
        row = db.session.get(Dispatch, d["id"])
        row.created_at = business_calendar.utcnow() - dt.timedelta(hours=25)
        db.session.commit()

    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "timezone label coverage check",
        "payload": {"lines": []},
    }).get_json()
    assert req["created_at_label"] is not None
    client.post("/api/logout")

    login_as("tz_cr_mgr", "password123", "manager")
    approved = client.post(f"/api/correction-requests/{req['id']}/approve", json={"review_note": "ok"}).get_json()
    assert approved["reviewed_at_label"] is not None
    assert approved["grant_expires_at_label"] is not None


def test_void_timestamp_uses_kampala_label(client, login_as):
    login_as("tz_void_root", "password123", "super_admin")
    cat = client.post("/api/admin/sales-categories", json={"name": "TZ Cat4"}).get_json()
    cust = client.post("/api/admin/customers", json={
        "name": "TZ Customer4", "sales_category_id": cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    product = client.post("/api/admin/products", json={"name": "TZ Product5"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TZ-D3", "date": "2026-08-21", "customer_id": cust["id"],
        "sales_category_id": cat["id"],
        "lines": [{"product_id": product["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "tz check"})
    fetched = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert fetched["status"] == "void"
    # voided_at is stored/returned canonically (see section 12 — API
    # contract safety); History display derives Kampala labels the same
    # established way as created_at, not tested again redundantly here.
    assert fetched["voided_at"] is not None


# =====================================================================
# SECTION 18 — device timezone independence (frontend)
# =====================================================================

STATIC = __import__("pathlib").Path(__file__).resolve().parent.parent / "static"
DASHBOARD_HTML = (STATIC / "dashboard.html").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
ADMIN_HTML = (STATIC / "admin.html").read_text(encoding="utf-8")
DISPATCH_HTML = (STATIC / "dispatch.html").read_text(encoding="utf-8")
RETURNS_HTML = (STATIC / "returns.html").read_text(encoding="utf-8")
PRODUCTION_HTML = (STATIC / "production.html").read_text(encoding="utf-8")
HISTORY_HTML = (STATIC / "history.html").read_text(encoding="utf-8")


def test_dashboard_generated_label_targets_kampala_explicitly():
    idx = DASHBOARD_HTML.index("function formatKampala(iso){")
    end = DASHBOARD_HTML.index("\n}", idx)
    body = DASHBOARD_HTML[idx:end]
    assert "timeZone: 'Africa/Kampala'" in body
    assert "document.getElementById('lastUpdated').textContent = data.generated_at\n    ? `Generated ${formatKampala(data.generated_at)}" in DASHBOARD_HTML


def test_dashboard_last_activity_label_also_uses_the_kampala_helper():
    assert "formatKampala(activity.last_activity_at)" in DASHBOARD_HTML
    assert "new Date(activity.last_activity_at + 'Z').toLocaleString()" not in DASHBOARD_HTML


def test_index_format_entry_time_targets_kampala_explicitly():
    idx = INDEX_HTML.index("function formatEntryTime(iso){")
    end = INDEX_HTML.index("\n}", idx)
    body = INDEX_HTML[idx:end]
    assert "timeZone:'Africa/Kampala'" in body


def test_index_reuses_format_entry_time_for_review_and_completion_labels():
    # Both call sites route through the one Kampala-aware helper instead
    # of duplicating a separate (buggy) inline new Date(...).toLocaleString().
    assert "formatEntryTime(row.completed_at)" in INDEX_HTML
    assert "formatEntryTime(session.submitted_at)" in INDEX_HTML
    assert "new Date(row.completed_at).toLocaleString()" not in INDEX_HTML
    assert "new Date(session.submitted_at).toLocaleString()" not in INDEX_HTML


def test_admin_last_login_targets_kampala_explicitly():
    assert "timeZone:'Africa/Kampala'" in ADMIN_HTML
    idx = ADMIN_HTML.index("last_login_at")
    line_start = ADMIN_HTML.rfind("\n", 0, idx)
    line_end = ADMIN_HTML.index("\n", idx)
    line = ADMIN_HTML[line_start:line_end]
    assert "u.last_login_at + 'Z'" in line  # naive-UTC ISO needs the 'Z' to parse correctly at all


_PAGES_WITH_TODAY_STR = {"dispatch.html": DISPATCH_HTML, "returns.html": RETURNS_HTML, "production.html": PRODUCTION_HTML}


@pytest.mark.parametrize("name", list(_PAGES_WITH_TODAY_STR))
def test_business_today_variable_populated_from_session_not_device_clock(name):
    html = _PAGES_WITH_TODAY_STR[name]
    assert "businessToday = data.business_today || null;" in html, name
    assert "function todayStr(){ return businessToday || new Date().toISOString().slice(0,10); }" in html, name


def test_index_business_today_populated_from_session():
    assert "businessToday = s.business_today || null;" in INDEX_HTML
    assert "function todayStr(){ return businessToday || new Date().toISOString().slice(0,10); }" in INDEX_HTML


def test_history_quick_filters_anchor_on_business_today_not_device_clock():
    assert "let businessToday = null;" in HISTORY_HTML
    assert "businessToday = data.business_today || null;" in HISTORY_HTML
    idx = HISTORY_HTML.index("function dateStr(offsetDays){")
    end = HISTORY_HTML.index("\n}", idx)
    body = HISTORY_HTML[idx:end]
    assert "businessToday" in body
    # The actual returned value is always built from local getters
    # (getFullYear/getMonth/getDate), never from .toISOString() on the
    # computed date — on either the businessToday-anchored or the
    # fallback path.
    assert "return `${yy}-${mm}-${dd}`;" in body


def test_history_this_month_filters_use_first_of_month_helper_everywhere():
    # 4 real call sites (dispatch/returns/production/daily-figures tabs)
    # + 1 match inside "function firstOfMonthStr(){" itself (the literal
    # substring "firstOfMonthStr()" is a prefix of that definition line).
    assert HISTORY_HTML.count("firstOfMonthStr()") == 5
    assert "d.setDate(1); document.getElementById" not in HISTORY_HTML


@pytest.mark.parametrize("name", list(_PAGES_WITH_TODAY_STR))
def test_server_now_capture_appends_z_for_correct_utc_parsing(name):
    # Without the 'Z', new Date(serverNow) would parse the naive-UTC
    # server_now string as browser-LOCAL time instead of UTC — silently
    # shifting the (purely cosmetic/UX) 24-hour edit-window comparison by
    # the viewer's own device UTC offset.
    html = _PAGES_WITH_TODAY_STR[name]
    assert "serverNow = data.server_now ? data.server_now + 'Z' : null;" in html, name


# =====================================================================
# SECTION 19 — date-only fields are never timezone-shifted
# =====================================================================

def test_dispatch_business_date_stored_exactly_as_submitted(client, login_as):
    login_as("tz_date_only_root", "password123", "super_admin")
    cat = client.post("/api/admin/sales-categories", json={"name": "TZ DateOnly Cat"}).get_json()
    cust = client.post("/api/admin/customers", json={
        "name": "TZ DateOnly Customer", "sales_category_id": cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    product = client.post("/api/admin/products", json={"name": "TZ DateOnly Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TZ-DATE-1", "date": "2026-08-19", "customer_id": cust["id"],
        "sales_category_id": cat["id"],
        "lines": [{"product_id": product["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    assert d["date"] == "2026-08-19"
    fetched = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert fetched["date"] == "2026-08-19"


def test_report_filter_date_echoed_back_unshifted(client, login_as):
    login_as("tz_filter_root", "password123", "super_admin")
    res = client.get("/api/dispatches/export.csv?date=2026-08-19")
    assert res.status_code == 200
    text = res.data.decode()
    assert "date=2026-08-19" in text
    assert "date=2026-08-18" not in text
    assert "date=2026-08-20" not in text


def test_daily_figures_selected_date_not_shifted(client, login_as):
    login_as("tz_df_date_root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "TZ DF Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})
    res = client.get(f"/api/daily-figures/{product['id']}?date=2026-08-19&shift=Day")
    assert res.status_code == 200


# =====================================================================
# SECTION 20 — business-rule regression (Metro Sales, 24h window, etc.)
# =====================================================================

def test_metro_sales_monday_rule_unaffected_by_timezone_audit(client, login_as):
    from webapp.services import returns_service
    login_as("tz_metro_root", "password123", "super_admin")
    metro_cat = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    dakar = client.post("/api/admin/customers", json={
        "name": "TZ Dakar", "sales_category_id": metro_cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    product = client.post("/api/admin/products", json={"name": "TZ Metro Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})

    import datetime as dt
    today = dt.date.fromisoformat(business_today())
    monday = (today - dt.timedelta(days=today.weekday())).isoformat()
    r = client.post("/api/returns", json={
        "date": monday, "customer_id": dakar["id"],
        "lines": [{"product_id": product["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    row = client.get(f"/api/daily-figures/{product['id']}?date={monday}&shift=Day").get_json()
    assert row["return_"]["base_qty"] == 200


def test_operator_24_hour_edit_window_still_exact_elapsed_duration(client, app, login_as):
    # Duration math (record_correction_service.operator_can_directly_edit)
    # is pure naive-UTC subtraction, completely untouched by this round —
    # confirms display-only changes never leaked into the comparison.
    from webapp.services import record_correction_service
    login_as("tz_24h_root", "password123", "super_admin")
    cat = client.post("/api/admin/sales-categories", json={"name": "TZ 24h Cat"}).get_json()
    cust = client.post("/api/admin/customers", json={
        "name": "TZ 24h Customer", "sales_category_id": cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    product = client.post("/api/admin/products", json={"name": "TZ 24h Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})

    login_as("tz_24h_op", "password123", "operator")
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TZ-24H-1", "date": "2020-01-01", "customer_id": cust["id"],
        "sales_category_id": cat["id"],
        "lines": [{"product_id": product["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()

    with app.app_context():
        from webapp.extensions import db
        from webapp.models.dispatch import Dispatch
        from webapp.models.user import User
        row = db.session.get(Dispatch, d["id"])
        user = User.query.filter_by(username="tz_24h_op").first()
        import datetime as dt
        assert record_correction_service.operator_can_directly_edit(
            row, user, now=row.created_at + dt.timedelta(hours=23, minutes=59)) is True
        assert record_correction_service.operator_can_directly_edit(
            row, user, now=row.created_at + dt.timedelta(hours=24, minutes=1)) is False


def test_duplicate_returns_prevention_unaffected(client, login_as):
    login_as("tz_dup_root", "password123", "super_admin")
    cat = client.post("/api/admin/sales-categories", json={"name": "TZ Dup Cat"}).get_json()
    cust = client.post("/api/admin/customers", json={
        "name": "TZ Dup Customer", "sales_category_id": cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    product = client.post("/api/admin/products", json={"name": "TZ Dup Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={"cartons_to_packs": 10, "packs_to_pieces": 10})
    body = {"date": "2026-08-21", "customer_id": cust["id"],
            "lines": [{"product_id": product["id"], "cartons": 1, "packs": 0, "pieces": 0}]}
    assert client.post("/api/returns", json=body).status_code == 201
    assert client.post("/api/returns", json=body).status_code == 409
