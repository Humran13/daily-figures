"""
Stage 3 frontend: company name/logo display across every page, and the
Company Settings admin panel. Source-level regression guards, same
rationale as every other frontend-only piece of this project (no
JS/browser test runner exists here).
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
PAGES = {
    "index.html": (STATIC_DIR / "index.html").read_text(encoding="utf-8"),
    "dispatch.html": (STATIC_DIR / "dispatch.html").read_text(encoding="utf-8"),
    "history.html": (STATIC_DIR / "history.html").read_text(encoding="utf-8"),
    "dashboard.html": (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8"),
    "admin.html": (STATIC_DIR / "admin.html").read_text(encoding="utf-8"),
}


def test_every_page_has_a_brand_name_slot():
    for name, source in PAGES.items():
        assert "data-brand-name" in source, f"{name} is missing a data-brand-name element"


def test_every_page_has_a_brand_logo_slot_hidden_by_default():
    for name, source in PAGES.items():
        assert 'data-brand-logo class="hidden"' in source, f"{name} is missing a hidden-by-default data-brand-logo element"


def test_every_page_fetches_public_branding_endpoint():
    for name, source in PAGES.items():
        assert "fetch('/api/branding')" in source, f"{name} does not call the public branding endpoint"


def test_every_page_calls_apply_branding_once_at_load():
    for name, source in PAGES.items():
        assert "async function applyBranding()" in source, f"{name} is missing applyBranding()"
        assert source.count("applyBranding();") == 1, f"{name} must call applyBranding() exactly once, not per-interaction"


def test_branding_fetch_failure_never_throws_uncaught():
    for name, source in PAGES.items():
        # cosmetic-only: a failed fetch must be caught, never propagate and
        # block the rest of page boot.
        idx = source.index("async function applyBranding()")
        snippet = source[idx:idx + 600]
        assert "try{" in snippet and "catch(e)" in snippet, f"{name}'s applyBranding() must not let a failed fetch throw"


# ---------- index.html: Daily Figures "Enter" and "History & Export" tabs ----------
# Regression coverage for a real bug: the login screen's data-brand-name/
# data-brand-logo elements were present and working, but the in-app header
# — the one shared by both #tab-entry and #tab-history, since it lives in
# <header>, a sibling of <main> rather than inside either tab's own div —
# never got its own brand-logo element, and the brand-name span had been
# merged into the "Daily Figures" heading text instead of the small
# brand-bar pattern every other page uses. A test that only checked "does
# this string appear somewhere in the file" (see test_every_page_has_a_*
# above) couldn't catch this, since the login screen's elements already
# satisfied it — hence the more targeted tests below.

def _count_html_elements_with_attr(source, attr):
    # Counts actual <tag ... attr ...> occurrences, not the JS selector
    # string ('[data-brand-name]') that applyBranding() itself contains.
    return len(re.findall(rf"<\w+[^>]*\b{re.escape(attr)}\b", source))


def test_index_html_has_two_distinct_brand_name_slots():
    """One for the login screen, one for the shared in-app header (used by
    both Enter and History & Export) — not merged into either."""
    source = PAGES["index.html"]
    count = _count_html_elements_with_attr(source, "data-brand-name")
    assert count == 2, (
        f"expected exactly 2 data-brand-name elements in index.html (login screen + "
        f"app header), found {count}"
    )


def test_index_html_has_two_distinct_brand_logo_slots():
    source = PAGES["index.html"]
    count = _count_html_elements_with_attr(source, "data-brand-logo")
    assert count == 2, (
        f"expected exactly 2 data-brand-logo elements in index.html (login screen + "
        f"app header), found {count}"
    )


def test_index_html_app_header_brand_bar_is_shared_by_both_tabs():
    source = PAGES["index.html"]
    header_start = source.index("<header>")
    main_start = source.index("<main>")
    header_block = source[header_start:main_start]
    assert "data-brand-name" in header_block, "the in-app header is missing a brand-name element"
    assert "data-brand-logo" in header_block, "the in-app header is missing a brand-logo element"
    # <header> is a sibling of <main> (which contains both #tab-entry and
    # #tab-history) — confirms this brand bar isn't nested inside just one
    # of the two tabs, so switching tabs never hides it.
    assert 'id="tab-entry"' not in header_block
    assert 'id="tab-history"' not in header_block


def test_index_html_product_heading_stays_fixed_text_not_overwritten_by_branding():
    """The "Daily Figures" product-identity heading must stay static text —
    branding lives in its own small brand-bar, consistent with every other
    page's header (dispatch.html says "Dispatch", history.html says
    "History & Exports", etc. — none of those page titles get replaced by
    the company name either)."""
    source = PAGES["index.html"]
    assert '<h1>Daily Figures <span class="logout" id="logoutBtn">Log out</span></h1>' in source


# ---------- dispatch.html print letterhead ----------

def test_print_letterhead_hidden_on_screen_shown_only_in_print():
    source = PAGES["dispatch.html"]
    assert ".print-letterhead{ display:none; }" in source
    assert "@media print {" in source
    print_block_start = source.index("@media print {")
    print_block = source[print_block_start:print_block_start + 400]
    assert ".print-letterhead{ display:flex !important;" in print_block


def test_print_letterhead_populated_from_branding_cache():
    source = PAGES["dispatch.html"]
    assert "function renderPrintLetterhead()" in source
    assert "renderPrintLetterhead();" in source  # called from applyBranding()


def test_print_letterhead_element_present_in_detail_tab():
    source = PAGES["dispatch.html"]
    assert 'id="printLetterhead"' in source


# ---------- admin.html Company Settings panel ----------

def test_admin_has_company_settings_tab():
    source = PAGES["admin.html"]
    assert '<div class="tab" data-panel="company">Company Settings</div>' in source
    assert 'id="panel-company"' in source


def test_admin_company_settings_form_has_all_required_fields():
    source = PAGES["admin.html"]
    for field_id in (
        "csDisplayName", "csLegalName", "csAddress", "csPhone", "csEmail",
        "csWebsite", "csCurrency", "csTaxNumber", "csContact", "csFooter",
    ):
        assert f'id="{field_id}"' in source, f"Company Settings form is missing #{field_id}"


def test_admin_has_logo_upload_preview_replace_remove_controls():
    source = PAGES["admin.html"]
    assert 'id="logoFileInput"' in source
    assert 'accept="image/png,image/jpeg,image/webp"' in source
    assert 'id="logoPreview"' in source
    assert 'id="uploadLogoBtn"' in source  # doubles as replace when a logo already exists
    assert 'id="removeLogoBtn"' in source


def test_admin_logo_upload_uses_multipart_form_data_not_json():
    source = PAGES["admin.html"]
    idx = source.index("uploadLogoBtn').addEventListener")
    snippet = source[idx:idx + 500]
    assert "FormData()" in snippet
    assert "form.append('logo', file)" in snippet
    assert "'Content-Type'" not in snippet  # never force JSON content-type on a file upload


def test_admin_remove_logo_confirms_before_deleting():
    source = PAGES["admin.html"]
    idx = source.index("removeLogoBtn').addEventListener")
    snippet = source[idx:idx + 400]
    assert "confirm(" in snippet


def test_admin_loads_company_settings_on_boot():
    source = PAGES["admin.html"]
    assert "loadCompanySettings()" in source
    boot_idx = source.index("// ---------- boot ----------")
    assert "loadCompanySettings()" in source[boot_idx:]
