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
APP_SHELL_JS = (STATIC_DIR / "app-shell.js").read_text(encoding="utf-8")

# Stage 6 centralized every page's in-app brand slot into the shared,
# dynamically-rendered identity bar (static/app-shell.js's
# renderIdentityBar(), into each page's #appIdentityBar placeholder) instead
# of a static data-brand-name/data-brand-logo pair baked into each page's
# own HTML — only index.html's login screen (rendered before any session
# exists, so app-shell.js has nothing to show yet) still has a static one
# of its own. See tests/test_stage6_app_shell.py for the shared shell's own
# coverage.
PAGES_WITH_STATIC_BRAND_SLOT = {"index.html": PAGES["index.html"]}


def test_every_page_has_a_brand_name_slot():
    for name, source in PAGES_WITH_STATIC_BRAND_SLOT.items():
        assert "data-brand-name" in source, f"{name} is missing a data-brand-name element"
    assert "setAttribute('data-brand-name', '')" in APP_SHELL_JS


def test_every_page_has_a_brand_logo_slot_hidden_by_default():
    for name, source in PAGES_WITH_STATIC_BRAND_SLOT.items():
        assert 'data-brand-logo class="hidden"' in source, f"{name} is missing a hidden-by-default data-brand-logo element"
    # app-shell.js's identity-bar logo starts hidden the same way (a
    # 'hidden' class, only removed once applyBranding() confirms a logo_url
    # exists) — see the shared page's own applyBranding() for that check.
    assert "logo.className = 'hidden';" in APP_SHELL_JS
    assert "setAttribute('data-brand-logo', '')" in APP_SHELL_JS


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


def test_index_html_has_one_static_brand_name_slot_for_the_login_screen():
    """Only the login screen needs a static brand slot baked into
    index.html's own markup — the in-app header's brand slot (used by both
    Enter and History & Export, since it lives in #appIdentityBar, a
    sibling of <main> rather than nested inside either tab) is rendered
    dynamically by static/app-shell.js once a session exists, same as
    every other page. See test_app_shell_identity_bar_renders_brand_slot
    below."""
    source = PAGES["index.html"]
    count = _count_html_elements_with_attr(source, "data-brand-name")
    assert count == 1, f"expected exactly 1 static data-brand-name element in index.html (login screen), found {count}"


def test_index_html_has_one_static_brand_logo_slot_for_the_login_screen():
    source = PAGES["index.html"]
    count = _count_html_elements_with_attr(source, "data-brand-logo")
    assert count == 1, f"expected exactly 1 static data-brand-logo element in index.html (login screen), found {count}"


def test_app_shell_identity_bar_renders_brand_slot_outside_any_single_tab():
    """The shared in-app header (#appIdentityBar) sits above <header>/<main>
    in every page's markup — a sibling of both, not nested inside either of
    index.html's own Enter/History & Export tab divs — so app-shell.js's
    dynamically-rendered brand bar is never hidden by a tab switch."""
    source = PAGES["index.html"]
    identity_idx = source.index('id="appIdentityBar"')
    tab_entry_idx = source.index('id="tab-entry"')
    assert identity_idx < tab_entry_idx, "#appIdentityBar must appear before the tab content, not nested inside it"
    assert "renderIdentityBar(identityContainer" in APP_SHELL_JS
    assert "data-brand-name" in APP_SHELL_JS and "data-brand-logo" in APP_SHELL_JS


def test_index_html_product_heading_stays_fixed_text_not_overwritten_by_branding():
    """The "Daily Figures" product-identity heading must stay static text —
    branding lives in its own small brand-bar (now the shared identity bar
    rendered by app-shell.js), consistent with every other page's header
    (dispatch.html says "Dispatch", history.html says "History & Exports",
    etc. — none of those page titles get replaced by the company name
    either). Stage 6 also moved Log out out of this heading and into the
    shared identity bar's own button."""
    source = PAGES["index.html"]
    assert "<h1>Daily Figures</h1>" in source
    assert 'id="logoutBtn"' not in source


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
