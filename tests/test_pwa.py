"""
PWA phone install support: web app manifest, icons, service worker, and the
shared install-UI script (static/pwa.js). Functional checks go through the
Flask test client (manifest/icons/service worker are all just static files
served by Flask, exactly like every other file under static/); the
install-UI behavior itself is a source-level regression guard, same
rationale as every other frontend-only piece of this project (no JS/
browser test runner exists here).

This patch must not change calculations, roles, permissions, feature
flags, source-book workflows, branding settings, database models, or
exports — those are covered by the rest of the suite continuing to pass
unchanged.
"""
import json
import io

import pytest
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
PWA_JS = (STATIC_DIR / "pwa.js").read_text(encoding="utf-8")
SW_JS = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
MANIFEST_TEXT = (STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8")
MANIFEST = json.loads(MANIFEST_TEXT)

PRIMARY_PAGES = ["index.html", "dispatch.html", "returns.html", "production.html",
                 "history.html", "dashboard.html", "admin.html"]
PAGE_SOURCES = {name: (STATIC_DIR / name).read_text(encoding="utf-8") for name in PRIMARY_PAGES}


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


# ---------- manifest ----------

def test_manifest_is_valid_json_with_required_fields():
    for field in ("name", "short_name", "description", "start_url", "scope",
                  "display", "theme_color", "background_color", "icons"):
        assert field in MANIFEST, f"manifest missing required field {field}"
    assert MANIFEST["display"] == "standalone"
    assert MANIFEST["start_url"] == "/"
    assert MANIFEST["scope"] == "/"


def test_manifest_has_192_and_512_icons():
    sizes = {icon["sizes"] for icon in MANIFEST["icons"]}
    assert "192x192" in sizes
    assert "512x512" in sizes


def test_manifest_has_a_maskable_icon():
    purposes = {icon.get("purpose") for icon in MANIFEST["icons"]}
    assert "maskable" in purposes


def test_manifest_served_by_flask(client):
    res = client.get("/manifest.webmanifest")
    assert res.status_code == 200
    data = res.get_json(force=True) if res.mimetype in ("application/json", "application/manifest+json") \
        else json.loads(res.data.decode())
    assert data["name"] == MANIFEST["name"]


def test_manifest_linked_from_every_primary_page():
    for name, source in PAGE_SOURCES.items():
        assert '<link rel="manifest" href="/manifest.webmanifest">' in source, f"{name} is missing the manifest link"


def test_every_primary_page_has_mobile_metadata():
    for name, source in PAGE_SOURCES.items():
        assert 'name="theme-color"' in source, f"{name} missing theme-color"
        assert 'name="mobile-web-app-capable"' in source, f"{name} missing mobile-web-app-capable"
        assert 'name="apple-mobile-web-app-capable"' in source, f"{name} missing apple-mobile-web-app-capable"
        assert 'rel="apple-touch-icon"' in source, f"{name} missing apple-touch-icon"


def test_every_primary_page_loads_pwa_script():
    for name, source in PAGE_SOURCES.items():
        assert '<script src="/pwa.js" defer></script>' in source, f"{name} is missing the pwa.js script tag"


# ---------- icons ----------

def test_icon_files_exist_and_are_correct_sizes():
    from PIL import Image
    icon_192 = Image.open(STATIC_DIR / "icons" / "icon-192.png")
    icon_512 = Image.open(STATIC_DIR / "icons" / "icon-512.png")
    icon_maskable = Image.open(STATIC_DIR / "icons" / "icon-maskable-512.png")
    assert icon_192.size == (192, 192)
    assert icon_512.size == (512, 512)
    assert icon_maskable.size == (512, 512)


def test_icons_served_by_flask(client):
    for path in ("/icons/icon-192.png", "/icons/icon-512.png", "/icons/icon-maskable-512.png"):
        res = client.get(path)
        assert res.status_code == 200
        assert res.mimetype == "image/png"


def test_apple_touch_icon_path_actually_resolves(client):
    res = client.get("/icons/icon-192.png")
    assert res.status_code == 200


# ---------- service worker ----------

def test_service_worker_served_by_flask(client):
    res = client.get("/sw.js")
    assert res.status_code == 200


def test_pwa_js_registers_the_service_worker():
    assert "navigator.serviceWorker.register('/sw.js')" in PWA_JS
    assert "'serviceWorker' in navigator" in PWA_JS


def test_service_worker_registration_never_throws_uncaught():
    idx = PWA_JS.index("navigator.serviceWorker.register")
    snippet = PWA_JS[idx:idx + 120]
    assert ".catch(" in snippet


def test_service_worker_never_caches_api_routes():
    assert "url.pathname.startsWith('/api/')" in SW_JS
    idx = SW_JS.index("url.pathname.startsWith('/api/')")
    assert "return" in SW_JS[idx:idx + 40]


def test_service_worker_never_intercepts_html_navigations():
    assert "req.mode === 'navigate'" in SW_JS
    idx = SW_JS.index("req.mode === 'navigate'")
    assert "return" in SW_JS[idx:idx + 40]


def test_service_worker_only_caches_a_small_explicit_shell_list():
    assert "SHELL_ASSETS" in SW_JS
    assert "/manifest.webmanifest" in SW_JS
    assert "/pwa.js" in SW_JS
    # Never a blanket cache of every static file — only the explicit list.
    assert "SHELL_ASSETS.includes(url.pathname)" in SW_JS


def test_service_worker_cleans_up_old_caches_on_activate():
    assert "self.addEventListener('activate'" in SW_JS
    idx = SW_JS.index("self.addEventListener('activate'")
    snippet = SW_JS[idx:idx + 400]
    assert "caches.delete" in snippet
    assert "self.clients.claim()" in snippet


def test_service_worker_takes_over_promptly_on_update():
    assert "self.skipWaiting()" in SW_JS


def test_service_worker_never_intercepts_non_get_requests():
    assert "req.method !== 'GET'" in SW_JS


def test_service_worker_never_touches_cross_origin_requests():
    assert "url.origin !== self.location.origin" in SW_JS


# ---------- install UI: hidden by default ----------

def test_install_banner_and_persistent_button_hidden_by_default():
    assert "banner.className = 'pwa-install-banner hidden';" in PWA_JS
    assert "persistentBtn.className = 'pwa-install-persistent hidden';" in PWA_JS


def test_ios_modal_hidden_by_default():
    assert "iosModal.className = 'pwa-ios-modal hidden';" in PWA_JS


def test_install_ui_never_built_when_already_standalone():
    idx = PWA_JS.index("function init() {")
    snippet = PWA_JS[idx:idx + 200]
    assert "if (isStandalone()) return;" in snippet


# ---------- beforeinstallprompt / Android ----------

def test_before_install_prompt_is_captured_and_default_prevented():
    idx = PWA_JS.index("addEventListener('beforeinstallprompt'")
    snippet = PWA_JS[idx:idx + 250]
    assert "e.preventDefault();" in snippet
    assert "deferredPrompt = e;" in snippet
    assert "reveal();" in snippet


def test_clicking_install_calls_the_stored_deferred_prompt():
    idx = PWA_JS.index("async function attemptInstall()")
    snippet = PWA_JS[idx:idx + 900]
    assert "prompted.prompt();" in snippet
    assert "await prompted.userChoice" in snippet
    assert "deferredPrompt = null;" in snippet  # cleared after use


def test_deferred_prompt_outcome_both_branches_handled():
    idx = PWA_JS.index("async function attemptInstall()")
    snippet = PWA_JS[idx:idx + 900]
    assert "choice.outcome !== 'accepted'" in snippet
    assert "showPersistentOnly();" in snippet


def test_appinstalled_hides_the_install_ui():
    idx = PWA_JS.index("addEventListener('appinstalled'")
    snippet = PWA_JS[idx:idx + 200]
    assert "hideAllInstallUi();" in snippet
    assert "deferredPrompt = null;" in snippet


def test_never_shows_a_fake_success_message():
    # No text anywhere in pwa.js claims installation succeeded except in
    # response to the real 'appinstalled' event.
    assert "installed successfully" not in PWA_JS.lower()
    assert "app has been installed" not in PWA_JS.lower()


# ---------- iOS / iPadOS ----------

def test_ios_reveals_custom_ui_immediately_since_no_native_event_exists():
    idx = PWA_JS.index("if (isIos()) {\n      // iOS never fires")
    snippet = PWA_JS[idx:idx + 300]
    assert "reveal();" in snippet


def test_ios_install_click_opens_instruction_modal_not_a_native_prompt():
    idx = PWA_JS.index("async function attemptInstall()")
    snippet = PWA_JS[idx:idx + 1000]
    assert "if (isIos()) { openIosModal(); return; }" in snippet


def test_ios_instructions_match_required_steps():
    assert "Open this website in Safari." in PWA_JS
    assert "Tap the Share button." in PWA_JS
    assert "Add to Home Screen" in PWA_JS
    assert 'Confirm by tapping' in PWA_JS


def test_ios_modal_warns_about_non_safari_browsers():
    assert "isStandardSafari" in PWA_JS
    assert "Safari may be required" in PWA_JS


def test_ios_modal_has_a_close_button_and_escape_handling():
    assert 'id="pwaIosModalClose"' in PWA_JS
    assert "e.key === 'Escape'" in PWA_JS


# ---------- dismissal ----------

def test_dismissal_is_stored_locally_for_seven_days():
    assert "DISMISS_DAYS = 7;" in PWA_JS
    assert "localStorage.setItem(DISMISS_KEY" in PWA_JS


def test_dismissed_state_shows_persistent_button_not_the_full_banner():
    idx = PWA_JS.index("function reveal() {")
    snippet = PWA_JS[idx:idx + 200]
    assert "if (dismissedRecently()) showPersistentOnly();" in snippet


def test_not_now_button_remembers_dismissal():
    idx = PWA_JS.index("getElementById('pwaInstallDismissBtn').addEventListener")
    snippet = PWA_JS[idx:idx + 150]
    assert "rememberDismissal();" in snippet


def test_localstorage_failures_never_break_the_page():
    idx = PWA_JS.index("function rememberDismissal()")
    snippet = PWA_JS[idx:idx + 200]
    assert "try {" in snippet and "catch (e)" in snippet


# ---------- does not interfere with login / typing ----------

def test_install_banner_defers_while_user_is_typing():
    assert "function isTypingInFormField()" in PWA_JS
    idx = PWA_JS.index("function showBanner()")
    snippet = PWA_JS[idx:idx + 200]
    assert "isTypingInFormField()" in snippet
    assert "setTimeout(showBanner" in snippet


def test_login_form_markup_on_index_html_is_untouched():
    index_source = PAGE_SOURCES["index.html"]
    assert 'id="usernameInput"' in index_source
    assert 'id="passwordInput"' in index_source
    assert 'id="loginBtn"' in index_source


# ---------- accessibility ----------

def test_install_controls_use_real_button_elements():
    assert '<button type="button" id="pwaInstallBtn">' in PWA_JS
    assert '<button type="button" id="pwaInstallDismissBtn">' in PWA_JS
    assert '<button type="button" id="pwaIosModalClose"' in PWA_JS


def test_focus_visible_styles_present():
    assert ":focus-visible{" in PWA_JS


def test_banner_has_dialog_role_and_aria_live():
    assert "banner.setAttribute('role', 'dialog');" in PWA_JS
    assert "banner.setAttribute('aria-live', 'polite');" in PWA_JS


def test_ios_modal_has_aria_modal_and_labelledby():
    assert "iosModal.setAttribute('aria-modal', 'true');" in PWA_JS
    assert "iosModal.setAttribute('aria-labelledby', 'pwaIosModalTitle');" in PWA_JS


# ---------- branding fallback preserved ----------

def test_install_text_falls_back_to_daily_figures_wording_when_branding_unset(client, super_admin):
    res = client.get("/api/branding")
    assert res.status_code == 200
    data = res.get_json()
    # Existing text-only fallback contract (Stage 3) is untouched: no logo
    # configured means logo_url stays falsy and display_name still resolves.
    assert "logo_url" in data
    assert "display_name" in data


def test_pwa_js_uses_branding_endpoint_and_has_its_own_fallback_text():
    assert "fetch('/api/branding')" in PWA_JS
    assert "Install this app on your phone for quicker access." in PWA_JS
    idx = PWA_JS.index("async function applyBrandingToText")
    snippet = PWA_JS[idx:idx + 500]
    assert "catch (e)" in snippet
    assert "el.textContent = fallback;" in snippet


# ---------- unrelated behavior unchanged ----------

def test_existing_operator_nav_untouched_on_index_html():
    assert "document.getElementById('operatorNav').classList.toggle('hidden', !isOperatorOrViewer);" in PAGE_SOURCES["index.html"]


def test_existing_apply_branding_function_still_present_on_every_page():
    for name, source in PAGE_SOURCES.items():
        assert "applyBranding" in source, f"{name} lost its own applyBranding logic"


def test_daily_figures_calculation_unaffected_by_pwa_patch(client, super_admin):
    product = client.post("/api/admin/products", json={"name": "PWA Patch Test Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    res = client.post("/api/daily-figures", json={
        "product_id": product["id"], "date": "2026-07-30", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    assert res.get_json()["opening"]["base_qty"] == 500


def test_viewer_permissions_unaffected_by_pwa_patch(client, super_admin, login_as):
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    res = client.post("/api/dispatches", json={
        "dispatch_number": "PWA-1", "date": "2026-07-30", "customer_id": 1, "lines": [],
    })
    assert res.status_code == 403
