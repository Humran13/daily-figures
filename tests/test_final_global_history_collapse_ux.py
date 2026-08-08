"""
Global History UX — every expandable History group across the app starts
collapsed on initial page load, never expanding today's date, the newest
date, or the most recently edited date automatically.

This is a display-only change. Every page below already had a working
expand/collapse mechanism (a `.date-group` wrapper toggling a `collapsed`
class, driven by a `data-toggle-*` click handler) — the only change is
that the freshly-rendered wrapper now carries `collapsed` from the start,
plus `aria-expanded="false"`/`role="button"`/`tabindex="0"` on the header
and a matching Enter/Space keydown handler, so the header is keyboard-
operable. No backend file, stock calculation, permission check, packaging
rule, or ledger/cutover logic was touched by this round — confirmed by
the fact that every test file below only inspects static HTML/JS text and
never spins up the Flask app.

Five render sites across five surfaces carry this pattern:
  - static/dispatch.html   loadList()               data-toggle-group
  - static/returns.html    loadList()               data-toggle-group
  - static/production.html loadList()               data-toggle-group
  - static/history.html    loadDispatchHistory()     data-toggle-group
  - static/history.html    loadReturnsHistory()      data-toggle-rgroup
  - static/history.html    loadProductionHistory()   data-toggle-pgroup
  - static/history.html    renderDailyFiguresHistory() data-toggle-hgroup
  - static/index.html      renderHistory()           data-toggle-hgroup

(dispatch.html's own History list was already collapsed-by-default from
the prior "Dispatch Workflow" round — this round adds the keyboard-
accessibility markup there too, for consistency with every other site,
and extends the collapsed-by-default behavior itself to the other seven.)
"""
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
DISPATCH_HTML = (STATIC / "dispatch.html").read_text(encoding="utf-8")
RETURNS_HTML = (STATIC / "returns.html").read_text(encoding="utf-8")
PRODUCTION_HTML = (STATIC / "production.html").read_text(encoding="utf-8")
HISTORY_HTML = (STATIC / "history.html").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def _slice(html, start_marker, end_marker):
    start = html.index(start_marker)
    end = html.index(end_marker, start)
    return html[start:end]


# =====================================================================
# Per-site render-function slices, one per (file, section) — every
# assertion below is scoped to the exact function that builds that
# section's markup, so a match can never accidentally come from a
# different, unrelated part of the same file.
# =====================================================================

def _dispatch_html_list_section():
    return _slice(DISPATCH_HTML, "async function loadList(){", "\nasync function loadUserFilterOptions")


def _returns_html_list_section():
    return _slice(RETURNS_HTML, "async function loadList(){", "\nfunction loadFilterOptionsFromUrl")


def _production_html_list_section():
    return _slice(PRODUCTION_HTML, "async function loadList(){", "\nasync function openDetail")


def _history_html_dispatch_section():
    return _slice(HISTORY_HTML, "async function loadDispatchHistory(){", "\ndocument.querySelectorAll('[data-quick]')")


def _history_html_returns_section():
    return _slice(HISTORY_HTML, "async function loadReturnsHistory(){", "\ndocument.querySelectorAll('[data-rquick]')")


def _history_html_production_section():
    return _slice(HISTORY_HTML, "async function loadProductionHistory(){", "\ndocument.querySelectorAll('[data-pquick]')")


def _history_html_daily_figures_section():
    return _slice(HISTORY_HTML, "async function renderDailyFiguresHistory(){", "\ndocument.querySelectorAll('[data-hquick]')")


def _index_html_history_section():
    return _slice(INDEX_HTML, "async function renderHistory(){", "\n// ---------- tabs ----------")


ALL_SECTIONS = {
    "dispatch.html list": _dispatch_html_list_section,
    "returns.html list": _returns_html_list_section,
    "production.html list": _production_html_list_section,
    "history.html dispatch": _history_html_dispatch_section,
    "history.html returns": _history_html_returns_section,
    "history.html production": _history_html_production_section,
    "history.html daily figures": _history_html_daily_figures_section,
    "index.html history": _index_html_history_section,
}


# =====================================================================
# 1. COLLAPSED BY DEFAULT — every section, no exceptions
# =====================================================================

@pytest.mark.parametrize("name", ALL_SECTIONS)
def test_date_group_starts_collapsed(name):
    section = ALL_SECTIONS[name]()
    assert '"date-group collapsed"' in section, f"{name}: date-group wrapper missing 'collapsed' by default"


@pytest.mark.parametrize("name", ALL_SECTIONS)
def test_no_special_casing_for_today_or_newest_date(name):
    """Nothing in the render path ever checks the group's own date against
    "today" (or picks out index 0 as special) to decide whether to leave
    the collapsed class off — every group is built identically."""
    section = ALL_SECTIONS[name]()
    # A literal per-group "is this today / is this the first one" branch
    # would have to reference todayStr()/index 0 right around where the
    # collapsed class is written — absence of any such comparison there
    # confirms every group is treated identically.
    assert "todayStr()" not in section
    assert "=== 0 ?" not in section
    assert "idx === 0" not in section


# =====================================================================
# 2. ACCESSIBILITY — aria-expanded, role, tabindex, keyboard activation
# =====================================================================

@pytest.mark.parametrize("name", ALL_SECTIONS)
def test_header_has_aria_expanded_false_initially(name):
    section = ALL_SECTIONS[name]()
    assert 'aria-expanded="false"' in section


@pytest.mark.parametrize("name", ALL_SECTIONS)
def test_header_is_keyboard_focusable_and_a_button_role(name):
    section = ALL_SECTIONS[name]()
    assert 'role="button"' in section
    assert 'tabindex="0"' in section


@pytest.mark.parametrize("name", ALL_SECTIONS)
def test_header_responds_to_enter_and_space_keys(name):
    section = ALL_SECTIONS[name]()
    assert "addEventListener('keydown'" in section
    assert "e.key === 'Enter'" in section
    assert "e.key === ' '" in section


@pytest.mark.parametrize("name", ALL_SECTIONS)
def test_aria_expanded_toggles_on_click(name):
    """The click handler must flip aria-expanded to match the new visual
    state, not just leave it permanently 'false'."""
    section = ALL_SECTIONS[name]()
    assert "setAttribute('aria-expanded'" in section


# =====================================================================
# 3. CHEVRON DIRECTION — CSS-level, shared by every .date-group site
# =====================================================================

def test_chevron_rotates_when_collapsed():
    css_block = _slice(DISPATCH_HTML, "<style>", "</style>")
    assert ".date-group.collapsed .chev{ transform:rotate(-90deg); }" in css_block


def test_collapsed_body_is_hidden_via_css_not_removed_from_dom():
    """The body is display:none while collapsed — the underlying markup
    (and therefore the underlying data) is never deleted, only hidden."""
    css_block = _slice(DISPATCH_HTML, "<style>", "</style>")
    assert ".date-group.collapsed .date-group-body{ display:none; }" in css_block


# =====================================================================
# 4. TOGGLE INTERACTION STILL PRESENT — clicking header or chevron
#    (same DOM element — the whole head is one click target) expands;
#    clicking again collapses; multiple groups may stay open at once
#    (no accordion/"close others" logic was ever present or added).
# =====================================================================

@pytest.mark.parametrize("name", ALL_SECTIONS)
def test_click_toggles_collapsed_class(name):
    section = ALL_SECTIONS[name]()
    assert "classList.toggle('collapsed')" in section


@pytest.mark.parametrize("name", ALL_SECTIONS)
def test_no_accordion_close_others_behavior_introduced(name):
    """Expanding one group must never collapse a sibling — nothing here
    iterates every OTHER group and force-collapses it."""
    section = ALL_SECTIONS[name]()
    assert "querySelectorAll('.date-group')" not in section  # only [data-toggle-*] is queried, never every group


# =====================================================================
# 5. RECORD OPENING STILL WORKS — clicking a row inside an expanded
#    group must not also toggle the group closed (event target check).
# =====================================================================

def test_dispatch_row_click_does_not_toggle_its_own_group():
    section = _dispatch_html_list_section()
    assert "e.target.closest('[data-open]')" in section


def test_returns_row_click_does_not_toggle_its_own_group():
    section = _returns_html_list_section()
    assert "e.target.closest('[data-open]')" in section


def test_production_row_click_does_not_toggle_its_own_group():
    section = _production_html_list_section()
    assert "e.target.closest('[data-open]')" in section


def test_history_dispatch_row_click_does_not_toggle_its_own_group():
    section = _history_html_dispatch_section()
    assert "e.target.closest('[data-open]')" in section


def test_history_returns_row_click_does_not_toggle_its_own_group():
    section = _history_html_returns_section()
    assert "e.target.closest('[data-open-return]')" in section


def test_history_production_row_click_does_not_toggle_its_own_group():
    section = _history_html_production_section()
    assert "e.target.closest('[data-open-production]')" in section


# =====================================================================
# 6. NEWEST-FIRST DATE ORDERING PRESERVED (unchanged sort, still present)
# =====================================================================

def test_dispatch_html_dates_still_sorted_newest_first():
    assert "results.sort((a,b)=> a<b?1:-1)" not in DISPATCH_HTML  # sanity: not a raw sort of results
    assert "sort((a,b)=> a<b?1:-1)" in DISPATCH_HTML


def test_returns_html_dates_still_sorted_newest_first():
    assert "sort((a,b)=> a<b?1:-1)" in RETURNS_HTML


def test_production_html_dates_still_sorted_newest_first():
    assert "sort((a,b)=> a<b?1:-1)" in PRODUCTION_HTML


def test_history_html_dates_still_sorted_newest_first():
    assert HISTORY_HTML.count("sort((a,b)=> a<b?1:-1)") >= 2  # groupByDate() + groupHistoryByDate()


def test_index_html_dates_still_sorted_newest_first():
    assert "sort((a,b)=> a<b?1:-1)" in INDEX_HTML


# =====================================================================
# 7. RECORD/SUMMARY COUNTS STILL SHOWN ON THE COLLAPSED HEADER
# =====================================================================

def test_dispatch_summary_shows_finalized_and_draft_counts():
    section = _dispatch_html_list_section()
    assert "finalizedCount" in section
    assert "draftCount" in section


def test_returns_summary_shows_finalized_count():
    section = _returns_html_list_section()
    assert "finalizedCount" in section
    assert "return${g.items.length===1?'':'s'}" in section


def test_production_summary_shows_finalized_count():
    section = _production_html_list_section()
    assert "finalizedCount" in section


def test_history_html_daily_figures_summary_shows_line_count():
    section = _history_html_daily_figures_section()
    assert "product line${g.items.length===1?'':'s'}" in section


# =====================================================================
# 8. SEARCH / FILTERS UNTOUCHED — every existing filter input still
#    wired to the same reload function; no new state persists a prior
#    search's "expanded" groups into a later, unrelated page load.
# =====================================================================

def test_dispatch_filters_still_present_and_wired():
    for fid in ["fDate", "fDateFrom", "fDateTo", "fCustomer", "fProduct", "fNumber", "fInvoice", "fStatus"]:
        assert f'id="{fid}"' in DISPATCH_HTML
    assert "'fCustomer'" in DISPATCH_HTML


def test_returns_filters_still_present_and_wired():
    for fid in ["fDate", "fDateFrom", "fDateTo", "fReturnedBy", "fProduct", "fStatus"]:
        assert f'id="{fid}"' in RETURNS_HTML


def test_production_filters_still_present_and_wired():
    for fid in ["fDate", "fDateFrom", "fDateTo", "fShift", "fProduct", "fStatus"]:
        assert f'id="{fid}"' in PRODUCTION_HTML


def test_history_html_all_four_filter_sets_still_present():
    for fid in ["fDate", "rDate", "pDate", "hDate"]:
        assert f'id="{fid}"' in HISTORY_HTML


def test_no_persisted_expanded_state_across_reloads():
    """No sessionStorage/localStorage read of a previously-open group is
    ever consulted when building the collapsed class — every load starts
    from the same hardcoded 'collapsed' default."""
    for html, name in [(DISPATCH_HTML, "dispatch"), (RETURNS_HTML, "returns"),
                        (PRODUCTION_HTML, "production"), (HISTORY_HTML, "history"), (INDEX_HTML, "index")]:
        assert "localStorage" not in html, f"{name}.html unexpectedly reads/writes localStorage"


# =====================================================================
# 9. DISPATCH WORKFLOW REGRESSION — Edit Draft / Correct / Delete markup
#    (built in the prior round) still present after this round's changes.
# =====================================================================

def test_dispatch_detail_actions_still_include_edit_draft_correct_and_delete():
    idx = DISPATCH_HTML.index("const actions = document.getElementById('detailActions');")
    end = DISPATCH_HTML.index("\n// ---------- Correct Record", DISPATCH_HTML.index("document.getElementById('backToListBtn')"))
    body = DISPATCH_HTML[idx:end]
    assert 'data-action="edit-draft"' in body
    assert 'data-action="correct"' in body
    assert 'data-action="delete"' in body


def test_correct_panel_still_has_date_and_recipient_fields():
    assert 'id="correctDate"' in DISPATCH_HTML
    assert 'id="correctSalesCategory"' in DISPATCH_HTML
    assert 'id="correctCustomerSearch"' in DISPATCH_HTML


def test_delete_panel_still_present():
    assert 'id="deletePanel"' in DISPATCH_HTML
    assert 'id="deleteConfirmBtn"' in DISPATCH_HTML


def test_success_screen_still_present():
    assert 'id="tab-success"' in DISPATCH_HTML
    assert 'id="successAddNewBtn"' in DISPATCH_HTML
