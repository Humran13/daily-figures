"""
Full targeted UX / reporting / data-entry package — 8 items:

  1. iPhone PWA Home Screen icon (apple-touch-icon).
  2. Dashboard Issued figures sorted highest -> lowest (exact integer
     base_qty, never a formatted-string sort).
  3. Automatic entry time (created_at) shown in History/reporting only.
  4. One active Return per canonical recipient per business date.
  5. Daily Figures stock-anchor helper text removed; mobile layout
     tightened (CSS only — stock-anchor BACKEND behavior untouched).
  6. "Skip for now, return before submitting" -> "Skip to Submit" (jumps
     straight to the Submit/Review screen).
  7. Manager/Super Admin may submit past unreviewed products with an
     explicit confirmation; hard blockers remain unconditional.
  8. Obsolete "Unfinalized Drafts" dashboard section removed (UI only —
     backend support deliberately left in place, unread by any page).

None of this touches stock formulas, packaging rules, Ledger Cutover,
Metro Sales posting rules, permissions, audit logging, or the Round D
same-day/void/correction-request workflow — see the regression suite
this round also re-ran in full.
"""
import pathlib

import pytest

from webapp.services.business_calendar import business_today, format_kampala_datetime

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
DASHBOARD_HTML = (STATIC / "dashboard.html").read_text(encoding="utf-8")
DISPATCH_HTML = (STATIC / "dispatch.html").read_text(encoding="utf-8")
RETURNS_HTML = (STATIC / "returns.html").read_text(encoding="utf-8")
PRODUCTION_HTML = (STATIC / "production.html").read_text(encoding="utf-8")
HISTORY_HTML = (STATIC / "history.html").read_text(encoding="utf-8")
ALL_HTML_PAGES = {
    "admin.html", "dashboard.html", "dispatch.html", "history.html", "index.html",
    "production.html", "reset-daily-values.html", "returns.html",
}


@pytest.fixture
def super_admin(login_as):
    return login_as("pkg_root", "password123", "super_admin")


def _make_product(client, name="PKG Product", cartons_to_packs=10, packs_to_pieces=10):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": cartons_to_packs, "packs_to_pieces": packs_to_pieces,
    })
    return product


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client)
    category = client.post("/api/admin/sales-categories", json={"name": "PKG Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "PKG Recipient", "sales_category_id": category["id"], "confirm_not_duplicate": True,
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


# =====================================================================
# ITEM 1 — iPhone PWA Home Screen icon
# =====================================================================

def test_apple_touch_icon_link_present_on_every_page():
    for name in ALL_HTML_PAGES:
        html = (STATIC / name).read_text(encoding="utf-8")
        assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in html, name


def test_apple_touch_icon_file_exists_and_is_valid_opaque_png():
    from PIL import Image
    path = STATIC / "apple-touch-icon.png"
    assert path.exists()
    img = Image.open(path)
    assert img.format == "PNG"
    assert img.size[0] == img.size[1]  # square
    assert img.size[0] >= 120  # a real, non-trivial icon size
    rgba = img.convert("RGBA")
    # No transparency anywhere — iOS applies its own rounded mask/gloss to
    # apple-touch-icon and does not composite a source alpha channel
    # correctly, which is the documented root cause of the "generic/
    # funny" fallback icon on iOS Home Screen installs.
    alpha_channel = rgba.split()[3]
    assert alpha_channel.getextrema() == (255, 255)


def test_apple_touch_icon_served_at_root_by_flask_static_config(client):
    res = client.get("/apple-touch-icon.png")
    assert res.status_code == 200
    assert res.headers["Content-Type"] == "image/png"


def test_manifest_still_valid_json_and_android_icons_unregressed():
    import json
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    srcs = {icon["src"] for icon in manifest["icons"]}
    assert "/icons/icon-192.png" in srcs
    assert "/icons/icon-512.png" in srcs
    assert "/icons/icon-maskable-512.png" in srcs
    maskable = next(i for i in manifest["icons"] if i["purpose"] == "maskable")
    assert maskable["src"] == "/icons/icon-maskable-512.png"


def test_old_transparent_icon_no_longer_used_for_apple_touch_icon():
    # The regression itself: apple-touch-icon must not point back at the
    # transparent-cornered icon-192.png (still fine for Android, where it
    # remains referenced from manifest.webmanifest, untouched above).
    for name in ALL_HTML_PAGES:
        html = (STATIC / name).read_text(encoding="utf-8")
        assert 'rel="apple-touch-icon" href="/icons/icon-192.png"' not in html, name


# =====================================================================
# ITEM 2 — Dashboard Issued figures sorted highest -> lowest
# =====================================================================

def _seed_unsorted_products(client, setup, date):
    """3000 / 77 / 1028 / 200 / 8 cartons — deliberately unsorted input
    order, matching the report's own reproduction numbers exactly.
    Expected numeric order: 3000 > 1028 > 200 > 77 > 8."""
    qtys = [("A", 3000), ("B", 77), ("C", 1028), ("D", 200), ("E", 8)]
    for name, qty in qtys:
        p = _make_product(client, f"Sort-{name}")
        d = client.post("/api/dispatches", json={
            "dispatch_number": f"SORT-{name}", "date": date, "customer_id": setup["customer"]["id"],
            "sales_category_id": setup["category"]["id"],
            "lines": [{"product_id": p["id"], "cartons": qty, "packs": 0, "pieces": 0}],
        }).get_json()
        client.post(f"/api/dispatches/{d['id']}/finalize")
    return ["Sort-A", "Sort-C", "Sort-D", "Sort-B", "Sort-E"]  # expected descending order


def test_top_issued_products_sorted_highest_to_lowest_by_exact_quantity(client, setup):
    date = business_today()
    expected_order = _seed_unsorted_products(client, setup, date)
    dash = client.get(f"/api/dashboard?date={date}").get_json()
    names_in_order = [p["product_name"] for p in dash["top_products"]]
    assert names_in_order == expected_order[:5]
    quantities = [p["base_qty"] for p in dash["top_products"]]
    assert quantities == sorted(quantities, reverse=True)


def test_top_issued_products_sorted_before_limiting_not_after(client, setup):
    # Sorting happens in the SQL query itself (ORDER BY total DESC, then
    # LIMIT) — proven by seeding MORE than the preview limit and checking
    # the top 5 returned are truly the 5 highest, not an arbitrary first 5
    # then sorted.
    date = business_today()
    qtys = [10, 500, 5, 3000, 1, 200, 8, 1028]
    for i, qty in enumerate(qtys):
        p = _make_product(client, f"Limit-{i}")
        d = client.post("/api/dispatches", json={
            "dispatch_number": f"LIMIT-{i}", "date": date, "customer_id": setup["customer"]["id"],
            "sales_category_id": setup["category"]["id"],
            "lines": [{"product_id": p["id"], "cartons": qty, "packs": 0, "pieces": 0}],
        }).get_json()
        client.post(f"/api/dispatches/{d['id']}/finalize")
    dash = client.get(f"/api/dashboard?date={date}").get_json()
    top5 = [p["base_qty"] for p in dash["top_products"]]
    assert len(top5) == 5
    assert top5 == sorted([q * 100 for q in qtys], reverse=True)[:5]


def test_sales_category_groups_sorted_highest_to_lowest(client, setup):
    date = business_today()
    group_qtys = {"CatHigh": 3000, "CatLow": 8, "CatMid": 1028}
    for name, qty in group_qtys.items():
        cat = client.post("/api/admin/sales-categories", json={"name": name}).get_json()
        cust = client.post("/api/admin/customers", json={
            "name": f"Cust-{name}", "sales_category_id": cat["id"], "confirm_not_duplicate": True,
        }).get_json()
        d = client.post("/api/dispatches", json={
            "dispatch_number": f"CAT-{name}", "date": date, "customer_id": cust["id"],
            "sales_category_id": cat["id"],
            "lines": [{"product_id": setup["product"]["id"], "cartons": qty, "packs": 0, "pieces": 0}],
        }).get_json()
        client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.get(f"/api/reports/recipient-totals?date_from={date}&date_to={date}&group_by=category").get_json()
    names = [r["group_name"] for r in res if r["group_name"] in group_qtys]
    assert names == ["CatHigh", "CatMid", "CatLow"]
    totals = [r["total_issued_base_qty"] for r in res]
    assert totals == sorted(totals, reverse=True)


def test_recipient_groups_sorted_highest_to_lowest(client, setup):
    date = business_today()
    group_qtys = {"RecHigh": 3000, "RecLow": 8, "RecMid": 1028}
    for name, qty in group_qtys.items():
        cust = client.post("/api/admin/customers", json={
            "name": name, "sales_category_id": setup["category"]["id"], "confirm_not_duplicate": True,
        }).get_json()
        d = client.post("/api/dispatches", json={
            "dispatch_number": f"REC-{name}", "date": date, "customer_id": cust["id"],
            "sales_category_id": setup["category"]["id"],
            "lines": [{"product_id": setup["product"]["id"], "cartons": qty, "packs": 0, "pieces": 0}],
        }).get_json()
        client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.get(f"/api/reports/recipient-totals?date_from={date}&date_to={date}&group_by=recipient").get_json()
    names = [r["group_name"] for r in res if r["group_name"] in group_qtys]
    assert names == ["RecHigh", "RecMid", "RecLow"]


def test_per_group_products_sorted_highest_to_lowest(client, setup):
    date = business_today()
    products = {}
    for name, qty in [("PA", 3000), ("PB", 77), ("PC", 1028)]:
        p = _make_product(client, f"InGroup-{name}")
        products[name] = p
    d = client.post("/api/dispatches", json={
        "dispatch_number": "INGROUP-1", "date": date, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [
            {"product_id": products["PA"]["id"], "cartons": 3000, "packs": 0, "pieces": 0},
            {"product_id": products["PB"]["id"], "cartons": 77, "packs": 0, "pieces": 0},
            {"product_id": products["PC"]["id"], "cartons": 1028, "packs": 0, "pieces": 0},
        ],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.get(f"/api/reports/recipient-totals?date_from={date}&date_to={date}&group_by=recipient").get_json()
    row = next(r for r in res if r["group_id"] == setup["customer"]["id"])
    names = [p["product_name"] for p in row["products"]]
    assert names == ["InGroup-PA", "InGroup-PC", "InGroup-PB"]


def test_view_all_reuses_the_same_already_sorted_array_never_a_second_fetch():
    # renderPreviewSection()/renderGroupedIssued() both slice the SAME
    # already-fetched `items`/`rows` array for both the compact preview
    # and the "View all" modal — never a second, separately-sorted fetch,
    # and never a client-side re-sort of a formatted string.
    assert "const preview = items.slice(0, PREVIEW_LIMIT);" in DASHBOARD_HTML
    assert "openModal(opts.modalTitle, items.map(rowFn).join(''));" in DASHBOARD_HTML
    assert "const preview = rows.slice(0, PREVIEW_LIMIT);" in DASHBOARD_HTML
    assert "openModal(opts.modalTitle, rows.map(r => groupBlockHtml(r, hrefFor, false)).join(''));" in DASHBOARD_HTML
    # No client-side Array.prototype.sort() call in this file at all for
    # top_products/byCategory/byRecipient — confirms the frontend never
    # re-sorts what the backend already returns, in particular never by
    # the formatted quantity_label string.
    assert ".sort(" not in DASHBOARD_HTML.split("// ---------- 4. Top products row")[1].split("// ---------- 7.")[0]


# =====================================================================
# ITEM 3 — Automatic entry time in History/reporting only
# =====================================================================

def test_format_kampala_datetime_is_correct_and_platform_safe():
    import datetime
    assert format_kampala_datetime(datetime.datetime(2026, 8, 12, 0, 42)) == "12 Aug 2026, 3:42 AM"
    assert format_kampala_datetime(datetime.datetime(2026, 8, 12, 12, 0)) == "12 Aug 2026, 3:00 PM"
    assert format_kampala_datetime(datetime.datetime(2026, 8, 12, 21, 30)) == "13 Aug 2026, 12:30 AM"
    assert format_kampala_datetime(None) is None


def test_dispatch_history_shows_created_time(client, setup):
    date = business_today()
    d = client.post("/api/dispatches", json={
        "dispatch_number": "PKG-D1", "date": date, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    assert d["created_at_label"] is not None
    fetched = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert fetched["created_at_label"] == d["created_at_label"]
    assert "Entered ${escapeHtml(d.created_at_label" in DISPATCH_HTML
    assert "Time Entered: ${escapeHtml(data.created_at_label" in DISPATCH_HTML
    assert "Entered ${escapeHtml(d.created_at_label" in HISTORY_HTML


def test_returns_history_shows_created_time(client, setup):
    date = business_today()
    r = client.post("/api/returns", json={
        "date": date, "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    assert r["created_at_label"] is not None
    assert "Entered ${escapeHtml(r.created_at_label" in RETURNS_HTML
    assert "Time Entered: ${escapeHtml(data.created_at_label" in RETURNS_HTML
    assert "Entered ${escapeHtml(r.created_at_label" in HISTORY_HTML


def test_production_history_shows_created_time(client, setup):
    date = business_today()
    p = client.post("/api/production", json={
        "date": date, "shift": "Day", "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    assert p["created_at_label"] is not None
    assert "Entered ${r.created_at_label" in PRODUCTION_HTML or "Entered ${escapeHtml(r.created_at_label" in PRODUCTION_HTML
    assert "Time Entered: ${escapeHtml(data.created_at_label" in PRODUCTION_HTML


def test_entry_forms_do_not_gain_manual_time_fields():
    # No new time <input> anywhere — the operator never types this.
    for html, label in ((DISPATCH_HTML, "dispatch"), (RETURNS_HTML, "returns"), (PRODUCTION_HTML, "production")):
        assert 'type="time"' not in html, label
        assert 'id="entryTime"' not in html, label
        assert 'name="created_at"' not in html, label


def test_daily_figures_screen_does_not_gain_creation_time_columns():
    assert "created_at_label" not in INDEX_HTML
    assert "Time Entered" not in INDEX_HTML


def test_correction_does_not_overwrite_original_created_at(client, setup):
    date = business_today()
    d = client.post("/api/dispatches", json={
        "dispatch_number": "PKG-D2", "date": date, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    before_created_at = d["created_at"]
    before_label = d["created_at_label"]

    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fix quantity", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    after = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert after["created_at"] == before_created_at
    assert after["created_at_label"] == before_label


def test_stock_anchor_backend_behavior_unchanged_by_helper_text_removal(client, setup):
    # The removed sentence was UI-text only — Manager/Super Admin
    # correcting Opening Stock on a later, normally-derived period must
    # still become the new carry-forward anchor exactly as before.
    pid = setup["product"]["id"]
    date = business_today()
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": date, "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": date, "shift": "Day",
        "opening": {"cartons": 15, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "recount",
    })
    assert res.status_code == 200
    assert res.get_json()["opening"]["base_qty"] == 1500


# =====================================================================
# ITEM 4 — Prevent duplicate Returns for the same recipient/day
# =====================================================================

def test_same_recipient_same_date_draft_duplicate_rejected(client, setup):
    date = business_today()
    body = {"date": date, "customer_id": setup["customer"]["id"],
            "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}]}
    first = client.post("/api/returns", json=body)
    assert first.status_code == 201
    second = client.post("/api/returns", json=body)
    assert second.status_code == 409
    assert "already exists for" in second.get_json()["error"]


def test_same_recipient_same_date_finalized_duplicate_rejected(client, setup):
    date = business_today()
    body = {"date": date, "customer_id": setup["customer"]["id"],
            "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}]}
    first = client.post("/api/returns", json=body).get_json()
    client.post(f"/api/returns/{first['id']}/finalize")
    second = client.post("/api/returns", json=body)
    assert second.status_code == 409


def test_voided_prior_return_allows_legitimate_replacement(client, setup, super_admin):
    date = business_today()
    body = {"date": date, "customer_id": setup["customer"]["id"],
            "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}]}
    first = client.post("/api/returns", json=body).get_json()
    client.post(f"/api/returns/{first['id']}/finalize")
    client.post(f"/api/returns/{first['id']}/void", json={"reason": "entered in error"})

    replacement = client.post("/api/returns", json=body)
    assert replacement.status_code == 201


def test_different_recipient_same_date_allowed(client, setup):
    date = business_today()
    other_customer = client.post("/api/admin/customers", json={
        "name": "PKG Other Recipient", "sales_category_id": setup["category"]["id"], "confirm_not_duplicate": True,
    }).get_json()
    client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    res = client.post("/api/returns", json={
        "date": date, "customer_id": other_customer["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 201


def test_same_recipient_different_date_allowed(client, setup):
    import datetime
    date = business_today()
    other_date = (datetime.date.fromisoformat(date) - datetime.timedelta(days=1)).isoformat()
    client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    res = client.post("/api/returns", json={
        "date": other_date, "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 201


def test_forged_api_duplicate_rejected_server_side(client, setup, login_as):
    # Server-side enforcement — reached even from a plain Operator session
    # hitting the API directly, not merely a frontend validation step.
    date = business_today()
    login_as("pkg_dup_op", "password123", "operator")
    body = {"date": date, "customer_id": setup["customer"]["id"],
            "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}]}
    assert client.post("/api/returns", json=body).status_code == 201
    forged = client.post("/api/returns", json=body)
    assert forged.status_code == 409


def test_normal_returns_stock_posting_unaffected(client, setup):
    date = business_today()
    r = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 4, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    row = client.get(f"/api/daily-figures/{setup['product']['id']}?date={date}&shift=Day").get_json()
    assert row["return_"]["base_qty"] == 400


def test_free_text_recipient_returns_are_not_subject_to_the_duplicate_rule(client, setup):
    # No canonical customer_id -> no canonical uniqueness to check against
    # (existing free-text behavior preserved unchanged, per the explicit
    # instruction not to invent a fragile name-based rule).
    date = business_today()
    body = {"date": date, "returned_by_name": "Unregistered Truck Route",
            "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}]}
    first = client.post("/api/returns", json=body)
    assert first.status_code == 201
    second = client.post("/api/returns", json=body)
    assert second.status_code == 201  # both accepted — free text has no canonical identity to dedupe on


def test_dakar_metro_sales_duplicate_rejected_via_canonical_relationship(client, setup):
    metro_cat = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    dakar = client.post("/api/admin/customers", json={
        "name": "Dakar", "sales_category_id": metro_cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    date = business_today()
    body = {"date": date, "customer_id": dakar["id"],
            "lines": [{"product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}]}
    first = client.post("/api/returns", json=body)
    assert first.status_code == 201
    second = client.post("/api/returns", json=body)
    assert second.status_code == 409
    assert "Dakar" in second.get_json()["error"]


def test_derrick_metro_sales_duplicate_rejected_across_a_customer_merge(client, setup, super_admin):
    # Recorded under an alias, later merged into the canonical "Derrick"
    # customer — the duplicate check must resolve the merge, not just
    # compare raw customer_id equality.
    metro_cat = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    derrick = client.post("/api/admin/customers", json={
        "name": "Derrick", "sales_category_id": metro_cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    derrick_alias = client.post("/api/admin/customers", json={
        "name": "Derrick Metro Route", "sales_category_id": metro_cat["id"], "confirm_not_duplicate": True,
    }).get_json()

    date = business_today()
    first = client.post("/api/returns", json={
        "date": date, "customer_id": derrick_alias["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert first.status_code == 201

    # Merge the alias into the canonical Derrick record.
    merge_res = client.post(f"/api/admin/customers/{derrick_alias['id']}/merge", json={"target_customer_id": derrick["id"]})
    assert merge_res.status_code == 200

    second = client.post("/api/returns", json={
        "date": date, "customer_id": derrick["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert second.status_code == 409


def test_metro_monday_posting_rule_unaffected_by_duplicate_prevention(client, setup, super_admin):
    import datetime
    metro_cat = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    cust = client.post("/api/admin/customers", json={
        "name": "Metro Monday Truck", "sales_category_id": metro_cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    today = datetime.date.fromisoformat(business_today())
    monday = today - datetime.timedelta(days=today.weekday())
    date = monday.isoformat()

    r = client.post("/api/returns", json={
        "date": date, "customer_id": cust["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    row = client.get(f"/api/daily-figures/{setup['product']['id']}?date={date}&shift=Day").get_json()
    assert row["return_"]["base_qty"] == 300  # Monday: real contribution, unaffected by this round


def test_metro_non_monday_posting_rule_unaffected_by_duplicate_prevention(client, setup, super_admin):
    import datetime
    metro_cat = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    cust = client.post("/api/admin/customers", json={
        "name": "Metro NonMonday Truck", "sales_category_id": metro_cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    today = datetime.date.fromisoformat(business_today())
    non_monday = today
    while non_monday.weekday() == 0:
        non_monday += datetime.timedelta(days=1)
    date = non_monday.isoformat()

    r = client.post("/api/returns", json={
        "date": date, "customer_id": cust["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    row = client.get(f"/api/daily-figures/{setup['product']['id']}?date={date}&shift=Day").get_json()
    assert row["return_"]["base_qty"] == 0  # non-Monday: still zero, unaffected by this round

    # And a second entry the same non-Monday date is still rejected.
    dup = client.post("/api/returns", json={
        "date": date, "customer_id": cust["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert dup.status_code == 409


# =====================================================================
# ITEM 5 — Helper text removed + mobile layout tightened
# =====================================================================

def test_stock_anchor_helper_sentence_removed_from_ui():
    assert "Correcting this becomes a new stock-balance anchor for every later date." not in INDEX_HTML
    assert "stock balance anchor" not in INDEX_HTML.lower().replace("-", " ")


def test_mobile_media_query_tightens_layout_without_touching_desktop_base_rules():
    assert "@media (max-width:430px){" in INDEX_HTML
    # Base (desktop/tablet) rules remain byte-for-byte present and
    # unchanged — the tightened values live only inside the media query.
    assert '.card{ background:white; border:1.5px solid var(--line); border-radius:16px; padding:22px 20px; margin-bottom:16px; }' in INDEX_HTML
    assert '.stock-readout{ display:flex; justify-content:space-between; align-items:center; background:var(--paper-dim); border-radius:10px; padding:12px 14px; margin-bottom:18px; }' in INDEX_HTML
    media_block = INDEX_HTML[INDEX_HTML.index("@media (max-width:430px){"):]
    media_block = media_block[:media_block.index("\n  }\n") + 5]
    assert ".card{ padding:14px 12px" in media_block
    assert ".stock-readout{ padding:9px 11px" in media_block


def test_no_inline_margin_style_left_on_stock_readout_rows():
    # The three previously-inline `style="margin-top:14px;"` occurrences
    # are now a plain `.mt` class, overridable by the media query above —
    # inline styles can't be overridden by any CSS selector.
    assert 'class="stock-readout mt"' in INDEX_HTML
    assert 'class="stock-readout" style="margin-top:14px;"' not in INDEX_HTML


def test_qty_inputs_remain_large_enough_to_tap_comfortably():
    # Font-size (which also prevents iOS auto-zoom on focus below 16px)
    # is untouched by the mobile tightening — only padding was reduced.
    assert ".qty-field input{ width:100%; text-align:center; font-family:var(--mono); font-weight:700; font-size:17px;" in INDEX_HTML


# =====================================================================
# ITEM 6 — "Skip to Submit"
# =====================================================================

def test_skip_review_button_relabeled_skip_to_submit():
    assert 'id="skipReviewBtn">Skip to Submit<' in INDEX_HTML
    assert "Skip for now — return before submitting" not in INDEX_HTML


def test_skip_review_jumps_directly_to_submit_screen_not_next_product():
    idx = INDEX_HTML.index("async function skipAndAdvanceReview(product, date, shift){")
    end = INDEX_HTML.index("\n}\n", idx)
    body = INDEX_HTML[idx:end]
    assert "showReviewScreen(date, shift);" in body
    assert "currentIdx++" not in body  # no longer advances product-by-product


def test_skip_to_submit_still_only_marks_the_current_product_skipped(client, setup, login_as):
    login_as("pkg_skip_mgr", "password123", "manager")
    pid = setup["product"]["id"]
    date = business_today()
    res = client.post("/api/daily-review/mark-skipped", json={"date": date, "shift": "Day", "product_id": pid})
    assert res.status_code == 200
    assert res.get_json()["state"] == "skipped"
    # No stock movement, no DailyFigure row created, no source-book record.
    view = client.get(f"/api/daily-figures/{pid}?date={date}&shift=Day").get_json()
    assert view["has_entry"] is False
    from webapp.models.dispatch import Dispatch
    from webapp.models.return_record import ReturnRecord
    from webapp.models.production_record import ProductionRecord
    assert Dispatch.query.count() == 0
    assert ReturnRecord.query.count() == 0
    assert ProductionRecord.query.count() == 0


def test_operator_skip_button_unchanged_by_this_round():
    # The Operator's own (non-elevated) Skip control keeps its original
    # label and per-product-advance behavior — only the Manager/Super
    # Admin review-flow Skip changed.
    assert 'id="skipBtn">Skip for now — leave this product unreviewed and come back later<' in INDEX_HTML


def test_back_and_next_navigation_still_works_normally():
    assert 'id="prevProductBtn">Previous Product<' in INDEX_HTML
    assert "currentIdx = parseInt(btn.dataset.gotoProduct, 10); renderEntryCard();" in INDEX_HTML  # "Return to this product" from the review screen


# =====================================================================
# ITEM 7 — Manager/Super Admin submit with unreviewed products
# =====================================================================

def test_manager_and_super_admin_submit_with_unreviewed_after_confirmation(client, setup, super_admin, login_as):
    date = business_today()
    other = _make_product(client, "PKG Unreviewed Other")
    # A second product created here too (while still super_admin, since
    # product creation is Super-Admin-only) for the Manager half below.
    mgr_product = _make_product(client, "PKG Unreviewed Mgr")
    client.post("/api/daily-review/mark-reviewed", json={
        "date": date, "shift": "Day", "product_id": setup["product"]["id"], "edited": False,
    })
    res = client.post("/api/daily-review/submit", json={"date": date, "shift": "Day", "force": True})
    assert res.status_code == 200

    # Same proof, but as a fresh Manager (not just Super Admin).
    client.post("/api/logout")
    login_as("pkg_submit_mgr", "password123", "manager")
    client.post("/api/daily-review/mark-reviewed", json={
        "date": "2026-01-05", "shift": "Day", "product_id": mgr_product["id"], "edited": False,
    })
    res2 = client.post("/api/daily-review/submit", json={"date": "2026-01-05", "shift": "Day", "force": True})
    assert res2.status_code == 200


def test_cancelling_the_confirmation_leaves_review_untouched():
    # Client-side: Cancel on either confirm() means no API call is made at
    # all — the handler returns immediately without calling apiPost.
    idx = INDEX_HTML.index("if(submitBtn) submitBtn.addEventListener('click', async ()=>{")
    end = INDEX_HTML.index("\n  });\n", idx)
    body = INDEX_HTML[idx:end]
    assert "if(!confirm(`Submit the Daily Figures review for" in body
    assert "if(!confirm(`${summary.unreviewed_count} product(s) have not been reviewed. Submit anyway?`)) return;" in body


def test_unreviewed_products_never_auto_zeroed_or_falsely_marked_reviewed(client, setup, super_admin):
    date = business_today()
    other = _make_product(client, "PKG Untouched Product")
    client.post("/api/daily-review/mark-reviewed", json={
        "date": date, "shift": "Day", "product_id": setup["product"]["id"], "edited": False,
    })
    client.post("/api/daily-review/submit", json={"date": date, "shift": "Day", "force": True})

    summary = client.get(f"/api/daily-review?date={date}&shift=Day").get_json()
    row = next(r for r in summary["products"] if r["product_id"] == other["id"])
    assert row["review_state"] == "not_reviewed"
    from webapp.models.daily_figure import DailyFigure
    assert DailyFigure.query.filter_by(product_id=other["id"], date=date).count() == 0


def test_stock_calculations_unchanged_by_force_submitting(client, setup, super_admin):
    date = business_today()
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": date, "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    other = _make_product(client, "PKG Force Stock Other")
    client.post("/api/daily-review/mark-reviewed", json={"date": date, "shift": "Day", "product_id": pid, "edited": False})
    before = client.get(f"/api/daily-figures/{pid}?date={date}&shift=Day").get_json()

    client.post("/api/daily-review/submit", json={"date": date, "shift": "Day", "force": True})

    after = client.get(f"/api/daily-figures/{pid}?date={date}&shift=Day").get_json()
    assert after["closing"]["base_qty"] == before["closing"]["base_qty"] == 1000


def test_operator_cannot_bypass_review_gate_at_all(client, setup, login_as):
    date = business_today()
    login_as("pkg_op_review", "password123", "operator")
    res = client.post("/api/daily-review/submit", json={"date": date, "shift": "Day", "force": True})
    assert res.status_code == 403


def test_viewer_remains_fully_read_only_on_review_endpoints(client, setup, login_as):
    date = business_today()
    login_as("pkg_viewer_review", "password123", "viewer")
    assert client.get(f"/api/daily-review?date={date}&shift=Day").status_code == 403
    assert client.post("/api/daily-review/submit", json={"date": date, "shift": "Day", "force": True}).status_code == 403
    assert client.post("/api/daily-review/mark-reviewed", json={
        "date": date, "shift": "Day", "product_id": setup["product"]["id"], "edited": False,
    }).status_code == 403


# =====================================================================
# ITEM 8 — Unfinalized Drafts section removed
# =====================================================================

def test_unfinalized_drafts_section_absent_from_dashboard_markup():
    assert "Unfinalized drafts" not in DASHBOARD_HTML
    assert 'id="draftList"' not in DASHBOARD_HTML
    assert "function draftRow(" not in DASHBOARD_HTML


def test_existing_draft_dispatch_records_are_completely_untouched(client, setup, super_admin):
    d = client.post("/api/dispatches", json={
        "dispatch_number": "PKG-DRAFT-1", "date": business_today(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    before = client.get(f"/api/dispatches/{d['id']}").get_json()

    # Loading the dashboard (which no longer surfaces drafts in its UI)
    # must not alter the draft record in any way.
    client.get(f"/api/dashboard?date={business_today()}")

    after = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert after == before
    assert after["status"] == "draft"


def test_draft_edit_and_void_still_work_after_drafts_section_removal(client, setup, login_as):
    login_as("pkg_draft_op", "password123", "operator")
    d = client.post("/api/dispatches", json={
        "dispatch_number": "PKG-DRAFT-2", "date": business_today(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    edit_res = client.patch(f"/api/dispatches/{d['id']}", json={"notes": "still editable"})
    assert edit_res.status_code == 200
    void_res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "cancel draft"})
    assert void_res.status_code == 200
    assert client.get(f"/api/dispatches/{d['id']}").get_json()["status"] == "void"


def test_draft_backend_support_deliberately_left_in_place(client, setup, super_admin):
    # The GET /api/dashboard `draft_dispatches` field is intentionally
    # NOT removed from the backend (harmless, simply unread by the
    # current UI) — see the completion report for why removing it
    # entirely was not judged "clearly safe" to do in the same pass.
    client.post("/api/dispatches", json={
        "dispatch_number": "PKG-DRAFT-3", "date": business_today(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    dash = client.get(f"/api/dashboard?date={business_today()}").get_json()
    assert "draft_dispatches" in dash
    assert dash["draft_dispatches"]["count"] >= 1
