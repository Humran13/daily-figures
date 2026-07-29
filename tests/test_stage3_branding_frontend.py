"""
Stage 3 frontend: company name/logo display across every page, and the
Company Settings admin panel. Source-level regression guards, same
rationale as every other frontend-only piece of this project (no
JS/browser test runner exists here).
"""
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
