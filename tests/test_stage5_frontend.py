"""
Stage 5 frontend: the mandatory "Opening Stock"/"Closing Stock" terminology
pass, the Closing Stock formula display, the new Returns/Production pages'
structural conventions, and their nav/branding wiring. Source-level
regression guards, same rationale as every other frontend-only piece of
this project (no JS/browser test runner exists here).
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
HISTORY_HTML = (STATIC_DIR / "history.html").read_text(encoding="utf-8")
DISPATCH_HTML = (STATIC_DIR / "dispatch.html").read_text(encoding="utf-8")
DASHBOARD_HTML = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
RETURNS_HTML = (STATIC_DIR / "returns.html").read_text(encoding="utf-8")
PRODUCTION_HTML = (STATIC_DIR / "production.html").read_text(encoding="utf-8")

USER_FACING_PAGES = {
    "index.html": INDEX_HTML,
    "history.html": HISTORY_HTML,
    "returns.html": RETURNS_HTML,
    "production.html": PRODUCTION_HTML,
}

FORBIDDEN_SHORTENED_LABELS = ["Opening Balance", "Closing Balance", "Start Stock", "End Stock"]


# ---------- mandatory terminology ----------

def test_index_html_uses_opening_stock_and_closing_stock_labels():
    assert "Opening Stock" in INDEX_HTML
    assert "Closing Stock" in INDEX_HTML


def test_no_shortened_stock_labels_anywhere_user_facing():
    for name, source in USER_FACING_PAGES.items():
        for label in FORBIDDEN_SHORTENED_LABELS:
            assert label not in source, f"{name} still contains the shortened label {label!r}"


def test_bare_opening_closing_headings_are_not_used_standalone():
    """The stock-readout/closing-readout labels must say the full "Opening
    Stock"/"Closing Stock", not a bare "Opening"/"Closing" — checked as
    whole label text inside the specific <span class="lbl"> elements that
    render them."""
    assert '<span class="lbl">Opening Stock</span>' in INDEX_HTML
    assert '<span class="lbl">Closing Stock</span>' in INDEX_HTML
    assert '<span class="lbl">Opening</span>' not in INDEX_HTML
    assert '<span class="lbl">Closing</span>' not in INDEX_HTML


def test_closing_stock_formula_displayed_with_exact_required_wording():
    assert "Closing Stock = Opening Stock + Production + Returns" in INDEX_HTML


def test_daily_figures_export_columns_use_stock_terminology():
    from webapp.routes.daily_figures import export_daily_figures  # noqa: F401 — import proves the module loads
    source = Path(__file__).resolve().parent.parent.joinpath("webapp", "routes", "daily_figures.py").read_text(encoding="utf-8")
    assert '"opening_stock", "Opening Stock"' in source
    assert '"closing_stock", "Closing Stock"' in source
    for label in FORBIDDEN_SHORTENED_LABELS:
        assert label not in source


# ---------- Return/Production are read-only in Daily Figures ----------

def test_daily_figures_entry_card_shows_return_and_production_as_readouts_not_inputs():
    assert "qtyInputsHtml('ret'" not in INDEX_HTML
    assert "qtyInputsHtml('prod'" not in INDEX_HTML
    assert "from the finalized Returns Book" in INDEX_HTML
    assert "from the finalized Production Book" in INDEX_HTML


# ---------- new pages: structural conventions reused from dispatch.html ----------

def test_returns_and_production_pages_have_new_and_list_tabs():
    assert 'data-tab="new">New Return</div>' in RETURNS_HTML
    assert 'data-tab="list">Returns</div>' in RETURNS_HTML
    assert 'data-tab="new">New Production</div>' in PRODUCTION_HTML
    assert 'data-tab="list">Production</div>' in PRODUCTION_HTML


def test_returns_and_production_pages_have_print_letterhead():
    assert 'id="printLetterhead"' in RETURNS_HTML
    assert 'id="printLetterhead"' in PRODUCTION_HTML


def test_returns_and_production_pages_apply_branding():
    assert "async function applyBranding()" in RETURNS_HTML
    assert "async function applyBranding()" in PRODUCTION_HTML
    assert "/api/branding" in RETURNS_HTML and "/api/branding" in PRODUCTION_HTML


def test_returns_and_production_pages_gate_writes_for_viewer():
    assert "function applyViewerReadOnly()" in RETURNS_HTML
    assert "function applyViewerReadOnly()" in PRODUCTION_HTML


def test_production_page_has_shift_selector_returns_page_does_not():
    assert '<select id="pShift">' in PRODUCTION_HTML
    assert "Day" in PRODUCTION_HTML and "Night" in PRODUCTION_HTML
    assert 'id="rShift"' not in RETURNS_HTML


def test_returns_page_has_signed_by_name_field():
    assert 'id="rSignedByName"' in RETURNS_HTML


# ---------- nav ----------

def test_dashboard_links_to_returns_and_production():
    assert 'href="/returns.html" data-module="returns"' in DASHBOARD_HTML
    assert 'href="/production.html" data-module="production"' in DASHBOARD_HTML


def test_history_admin_tier_nav_links_to_returns_and_production():
    admin_tier_nav = re.search(r'<div class="tabs" id="adminTierNav">.*?</div>', HISTORY_HTML, re.DOTALL).group(0)
    assert 'data-module="returns"' in admin_tier_nav
    assert 'data-module="production"' in admin_tier_nav


def test_returns_and_production_pages_tagged_for_page_guards():
    # webapp/routes/pages.py registers explicit /returns.html and
    # /production.html routes — these are just the corresponding
    # data-module fallback tags each page uses for its own nav links.
    assert 'data-module="dashboard"' in RETURNS_HTML
    assert 'data-module="dashboard"' in PRODUCTION_HTML
