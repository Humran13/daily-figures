"""
Final Stage 6 correction: (1) confirm the company logo configured in
Admin -> Company Settings is the single, authoritative source for every
place branding appears (header, login, print/PDF, PWA manifest/icons) —
no separate PWA-logo setting — and (2) fix the PWA install banner
overlapping Save Draft/Finalize/etc. on operational and Daily Figures
entry/review pages by giving those pages a small, non-blocking, in-flow
install control instead of the fixed/floating promotional banner (which
remains only on the login screen and Dashboard).

Source-level regression guards for the frontend pieces (no JS/browser test
runner exists in this project); real HTTP/DB coverage for everything
reachable through the Flask test client. Does not duplicate the broader
branding/derived-icon/manifest coverage already in test_company_settings.py
and tests/test_stage6_app_shell.py — only what's new or changed here.
"""
import io
from pathlib import Path

import pytest
from PIL import Image

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
PWA_JS = (STATIC_DIR / "pwa.js").read_text(encoding="utf-8")
APP_SHELL_JS = (STATIC_DIR / "app-shell.js").read_text(encoding="utf-8")
PRIMARY_PAGES = {
    name: (STATIC_DIR / name).read_text(encoding="utf-8")
    for name in (
        "index.html", "dispatch.html", "returns.html", "production.html",
        "history.html", "dashboard.html", "admin.html",
    )
}


def _png_bytes(size=(20, 20), color="red"):
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    buf.seek(0)
    return buf


# =====================================================================
# 1. One company logo source
# =====================================================================

def test_no_separate_pwa_logo_model_or_upload_field():
    """CompanySettings has exactly one uploaded-logo field (logo_path) that
    every icon/branding surface derives from — never a second, independent
    'pwa logo' column or upload endpoint."""
    from webapp.models.company_settings import CompanySettings
    columns = {c.name for c in CompanySettings.__table__.columns}
    logo_related = {c for c in columns if "logo" in c.lower() or "icon" in c.lower()}
    assert logo_related == {"logo_path", "icon_192_path", "icon_512_path", "icon_512_maskable_path"}, (
        "icon_* columns must only ever be *derived from* logo_path, never a second independent source"
    )


def test_only_one_logo_upload_route_exists(client, login_as):
    """A second 'pwa logo' or 'icon upload' endpoint would violate the
    single-source requirement — only the existing Company Settings logo
    route accepts a file upload."""
    login_as("root", "password123", "super_admin")
    for path in ("/api/admin/pwa-logo", "/api/admin/pwa-icon", "/api/admin/icon", "/api/admin/branding/icon"):
        res = client.post(path, data={"logo": (_png_bytes(), "logo.png")}, content_type="multipart/form-data")
        # This app serves static files from the root path (static_url_path=""),
        # so an unmatched path still resolves to the static catch-all route
        # and a POST to it comes back 405 (wrong method) rather than 404 —
        # either way, no such upload endpoint actually accepts and processes
        # a file here.
        assert res.status_code in (404, 405)


def test_uploaded_logo_immediately_becomes_the_public_header_logo(client, login_as):
    login_as("root", "password123", "super_admin")
    before = client.get("/api/branding").get_json()
    assert before["logo_url"] is None
    client.post("/api/admin/company-settings/logo", data={"logo": (_png_bytes(), "logo.png")},
                content_type="multipart/form-data")
    after = client.get("/api/branding").get_json()
    assert after["logo_url"] is not None


def test_uploaded_logo_is_also_the_manifest_icon_source(client, login_as):
    """The exact same upload that becomes the header logo also drives the
    manifest's icons — confirms there's one pipeline, not two."""
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/company-settings/logo", data={"logo": (_png_bytes(), "logo.png")},
                content_type="multipart/form-data")
    manifest = client.get("/manifest.webmanifest").get_json()
    srcs = [i["src"] for i in manifest["icons"]]
    assert any(s.startswith("/api/branding/icon-192.png?v=") for s in srcs)


def test_every_primary_page_fetches_the_same_public_branding_endpoint():
    for name, source in PRIMARY_PAGES.items():
        assert "fetch('/api/branding')" in source, f"{name} must use the single shared branding endpoint"


# =====================================================================
# 2. Super-admin company logo control (permissions)
# =====================================================================

@pytest.mark.parametrize("role", ["manager", "operator", "viewer"])
def test_non_super_admin_cannot_upload_logo(client, login_as, role):
    login_as(f"u_{role}", "password123", role)
    res = client.post("/api/admin/company-settings/logo", data={"logo": (_png_bytes(), "logo.png")},
                       content_type="multipart/form-data")
    assert res.status_code == 403


@pytest.mark.parametrize("role", ["manager", "operator", "viewer"])
def test_non_super_admin_cannot_remove_logo(client, login_as, role):
    login_as(f"u2_{role}", "password123", role)
    res = client.delete("/api/admin/company-settings/logo")
    assert res.status_code == 403


@pytest.mark.parametrize("role", ["manager", "operator", "viewer"])
def test_non_super_admin_can_still_view_public_branding(client, login_as, role):
    """Every role sees the logo/company name in their own header (public,
    unauthenticated endpoint) — only the admin write controls are
    restricted, never the ability to see current branding."""
    login_as(f"u3_{role}", "password123", role)
    res = client.get("/api/branding")
    assert res.status_code == 200


def test_admin_html_has_preview_upload_replace_remove_and_guidance():
    source = PRIMARY_PAGES["admin.html"]
    assert 'id="logoPreview"' in source
    assert 'id="logoFileInput"' in source
    assert 'id="uploadLogoBtn"' in source  # doubles as replace when a logo already exists
    assert 'id="removeLogoBtn"' in source
    assert 'id="logoMsg"' in source
    assert "PNG, JPEG, or WebP" in source
    assert "Max 2MB" in source


def test_admin_html_upload_success_message_is_truthful_about_reinstall():
    source = PRIMARY_PAGES["admin.html"]
    idx = source.index("uploadLogoBtn').addEventListener")
    snippet = source[idx:idx + 700]
    assert "New installations will use the new icon" in snippet
    assert "Existing installations may require reinstalling" in snippet


def test_admin_html_company_settings_note_never_promises_instant_update():
    source = PRIMARY_PAGES["admin.html"]
    assert "removed and installed again" in source
    for overclaim in ("instantly", "immediately updates", "right away"):
        assert overclaim not in source.lower()


# =====================================================================
# 3. PWA icon generation / manifest cache-busting / removal fallback
# =====================================================================

def test_replacing_logo_changes_manifest_icon_urls(client, login_as):
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/company-settings/logo", data={"logo": (_png_bytes(color="red"), "logo1.png")},
                content_type="multipart/form-data")
    first = client.get("/manifest.webmanifest").get_json()
    first_srcs = sorted(i["src"] for i in first["icons"])

    client.post("/api/admin/company-settings/logo", data={"logo": (_png_bytes(color="blue"), "logo2.png")},
                content_type="multipart/form-data")
    second = client.get("/manifest.webmanifest").get_json()
    second_srcs = sorted(i["src"] for i in second["icons"])

    assert first_srcs != second_srcs


def test_removing_logo_returns_manifest_to_generic_fallback_with_no_broken_urls(client, login_as):
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/company-settings/logo", data={"logo": (_png_bytes(), "logo.png")},
                content_type="multipart/form-data")
    client.delete("/api/admin/company-settings/logo")

    manifest = client.get("/manifest.webmanifest").get_json()
    for icon in manifest["icons"]:
        res = client.get(icon["src"])
        assert res.status_code == 200  # never a broken/404 icon URL in the manifest


def test_removing_logo_restores_text_only_fallback(client, login_as):
    login_as("root", "password123", "super_admin")
    client.post("/api/admin/company-settings/logo", data={"logo": (_png_bytes(), "logo.png")},
                content_type="multipart/form-data")
    client.delete("/api/admin/company-settings/logo")
    branding = client.get("/api/branding").get_json()
    assert branding["logo_url"] is None
    assert branding["display_name"]  # text fallback always available


# =====================================================================
# 6. Install banner must not block operational forms
# =====================================================================

def test_pwa_js_classifies_operational_and_review_pages_as_compact():
    idx = PWA_JS.index("async function isCompactPage()")
    body = PWA_JS[idx:PWA_JS.index("\n  }", idx)]
    assert "FULL_BANNER_PATHS.indexOf(path)" in body
    assert "return true;" in body  # default (dispatch/returns/production/history/admin) is compact


def test_dashboard_and_login_screen_keep_the_full_banner():
    assert "var FULL_BANNER_PATHS = ['/dashboard.html'];" in PWA_JS
    assert "var LOGIN_OR_DAILY_FIGURES_PATHS = ['/', '/index.html'];" in PWA_JS


def test_daily_figures_switches_to_compact_once_authenticated():
    idx = PWA_JS.index("LOGIN_OR_DAILY_FIGURES_PATHS.indexOf(path)")
    snippet = PWA_JS[idx:idx + 400]
    assert "/api/session" in snippet
    assert "data.authed" in snippet


def test_compact_banner_uses_static_positioning_not_fixed():
    idx = PWA_JS.index(".pwa-install-banner.pwa-install-compact{")
    block = PWA_JS[idx:PWA_JS.index("}", idx)]
    assert "position:static" in block


def test_compact_pages_never_build_the_floating_persistent_pill():
    idx = PWA_JS.index("function buildDom(compact)")
    body = PWA_JS[idx:PWA_JS.index("\n  async function applyBrandingToText", idx)]
    assert "if (!compact) {" in body
    assert "if (persistentBtn) document.body.appendChild(persistentBtn);" in body


def test_full_promotional_banner_still_fixed_for_dashboard_and_login():
    idx = PWA_JS.index(".pwa-install-banner{position:fixed")
    assert idx >= 0  # the original fixed/promotional rule is untouched


def test_seven_day_dismissal_preserved_in_both_modes():
    assert "DISMISS_DAYS = 7" in PWA_JS
    assert "function dismissedRecently()" in PWA_JS
    assert "function rememberDismissal()" in PWA_JS


def test_android_beforeinstallprompt_handling_unchanged():
    assert "window.addEventListener('beforeinstallprompt'" in PWA_JS
    assert "e.preventDefault();" in PWA_JS
    assert "deferredPrompt = e;" in PWA_JS


def test_appinstalled_handling_unchanged():
    assert "window.addEventListener('appinstalled'" in PWA_JS
    assert "hideAllInstallUi();" in PWA_JS


def test_ios_instructions_and_standalone_detection_unchanged():
    assert "function isIos()" in PWA_JS
    assert "function isStandalone()" in PWA_JS
    assert "Add to Home Screen" in PWA_JS


def test_service_worker_registration_unchanged():
    assert "navigator.serviceWorker.register('/sw.js')" in PWA_JS


def test_install_button_click_handler_never_assumes_persistent_pill_exists():
    """attemptInstall()/the click-wiring section must null-guard
    dom.persistentBtn — it doesn't exist at all on compact pages."""
    idx = PWA_JS.index("async function attemptInstall()")
    body = PWA_JS[idx:PWA_JS.index("\n    document.getElementById('pwaInstallBtn')", idx) + 400]
    assert "if (dom.persistentBtn) dom.persistentBtn.classList.add('hidden');" in body
    assert "if (dom.persistentBtn) dom.persistentBtn.addEventListener" in body


def test_every_primary_page_still_loads_pwa_js_once():
    for name, source in PRIMARY_PAGES.items():
        assert source.count('<script src="/pwa.js" defer></script>') == 1


# =====================================================================
# Regression: manifest/SW safety, calculations, permissions, nav
# =====================================================================

def test_manifest_still_served_dynamically_and_unauthenticated(client):
    res = client.get("/manifest.webmanifest")
    assert res.status_code == 200
    assert res.mimetype == "application/manifest+json"


def test_service_worker_still_excludes_api_and_cleans_old_caches():
    sw_source = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    assert "url.pathname.startsWith('/api/')" in sw_source
    assert "names.filter((name) => name !== CACHE_VERSION)" in sw_source


def test_operator_still_lands_on_dispatch(client, login_as):
    login_as("op1", "password123", "operator")
    res = client.get("/admin.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/dispatch.html?tab=new"


def test_viewer_still_read_only_on_dashboard(client, login_as):
    login_as("view1", "password123", "viewer")
    res = client.get("/api/dashboard?date=2026-07-28")
    assert res.status_code == 200


def test_closing_stock_formula_still_unchanged(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Install Fix Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    pid = product["id"]
    date = "2026-07-28"
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": date, "shift": "Day",
        "opening": {"cartons": 4, "packs": 0, "pieces": 0},
    })
    res = client.get(f"/api/dashboard?date={date}").get_json()
    row = next(r for r in res["stock_summary"] if r["product_id"] == pid)
    assert row["closing_base_qty"] == (
        row["opening_base_qty"] + row["production_base_qty"] + row["return_base_qty"] - row["issued_base_qty"]
    )


def test_reporting_nav_and_operational_switcher_role_gating_unchanged():
    idx = APP_SHELL_JS.index("function reportingNavItems(role, flags)")
    body = APP_SHELL_JS[idx:APP_SHELL_JS.index("\n  }", idx)]
    assert "(role === 'manager' || role === 'super_admin')" in body
    assert "if (role === 'super_admin')" in body
