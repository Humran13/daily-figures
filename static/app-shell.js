/*
 * Shared authenticated application shell — the one place role-aware
 * landing pages, the consistent header (branding/identity/Home/Back/
 * Logout), and the two navigation contexts (reporting vs operational) are
 * implemented, loaded identically by every primary page (see each page's
 * <script src="/app-shell.js" defer>, placed after that page's own
 * <script> block so window.applyBranding already exists by the time this
 * runs). Never duplicated per page — see Stage 6's "shared implementation"
 * requirement.
 *
 * Runs fully independently of each page's own boot()/init() — it does its
 * own /api/session and /api/feature-flags reads (the same endpoints every
 * page already calls) and renders into two placeholder elements every
 * primary page provides: #appIdentityBar and #appRoleNav. It never
 * touches a page's own internal tabs/content — those keep working exactly
 * as before.
 *
 * Does nothing at all when there is no authenticated session (e.g. the
 * login screen on "/") — both placeholders are simply left empty.
 */
(function () {
  'use strict';

  var ROLE_LABELS = {
    super_admin: 'Super Administrator',
    manager: 'Manager',
    operator: 'Operator',
    viewer: 'Viewer',
  };

  var BREADCRUMB_KEY = 'appNavStack';
  var BREADCRUMB_MAX = 20;

  function enabled(flags, key) {
    return flags[key] !== false; // a module with no row yet defaults to enabled, same as the backend
  }

  // Mirrors webapp/routes/pages.py's first_authorized_page() exactly —
  // keep the two in sync if either changes.
  function resolveLanding(role, flags) {
    if (role === 'operator') {
      if (enabled(flags, 'dispatch')) return '/dispatch.html?tab=new';
      if (enabled(flags, 'returns')) return '/returns.html?tab=new';
      if (enabled(flags, 'production')) return '/production.html?tab=new';
      return '/dispatch.html?tab=new';
    }
    if (enabled(flags, 'dashboard')) return '/dashboard.html';
    if (enabled(flags, 'daily_figures')) return '/';
    if (enabled(flags, 'history_exports')) return '/history.html';
    return '/dashboard.html';
  }

  function currentPageKey() {
    var path = location.pathname;
    if (path === '/' || path === '/index.html') return 'daily_figures';
    if (path === '/dispatch.html') return 'dispatch';
    if (path === '/returns.html') return 'returns';
    if (path === '/production.html') return 'production';
    if (path === '/history.html') return 'history_exports';
    if (path === '/dashboard.html') return 'dashboard';
    if (path === '/admin.html') return 'admin';
    return null;
  }

  var OPERATIONAL_PAGES = ['dispatch', 'returns', 'production'];

  // ---------- breadcrumb-based Back (never relies on document.referrer —
  // that's the exact "open redirect from an external referrer" risk the
  // spec calls out; this only ever navigates to a page THIS app itself
  // already recorded visiting, so an external site can never be the
  // target). Uses sessionStorage rather than browser history.back()
  // specifically so it also works predictably inside an installed
  // standalone PWA window. ----------
  function pushBreadcrumb() {
    try {
      var stack = JSON.parse(sessionStorage.getItem(BREADCRUMB_KEY) || '[]');
      var current = location.pathname + location.search;
      if (stack[stack.length - 1] !== current) stack.push(current);
      if (stack.length > BREADCRUMB_MAX) stack = stack.slice(-BREADCRUMB_MAX);
      sessionStorage.setItem(BREADCRUMB_KEY, JSON.stringify(stack));
    } catch (e) { /* private browsing / storage disabled — Back just falls back to Home */ }
  }

  function goBack(homeHref) {
    try {
      var stack = JSON.parse(sessionStorage.getItem(BREADCRUMB_KEY) || '[]');
      stack.pop(); // this page
      var prev = stack.pop(); // the page before it
      if (prev) {
        sessionStorage.setItem(BREADCRUMB_KEY, JSON.stringify(stack));
        location.href = prev;
        return;
      }
    } catch (e) { /* fall through to Home */ }
    location.href = homeHref;
  }

  // ---------- styles ----------
  function injectStyles() {
    if (document.getElementById('appShellStyles')) return;
    var style = document.createElement('style');
    style.id = 'appShellStyles';
    style.textContent =
      '.ash-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:var(--ink,#1B2430);' +
      'color:var(--paper,#FCFAF6);padding:8px 14px;font-family:inherit;}' +
      '.ash-brand{display:flex;align-items:center;gap:6px;min-width:0;flex:1 1 auto;}' +
      '.ash-brand img{height:16px;width:auto;}' +
      '.ash-brand span{font-size:11px;color:#8A94A6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}' +
      '.ash-identity{font-size:11px;color:#C7CDD6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:40vw;}' +
      '.ash-identity .ash-role{color:#8A94A6;}' +
      '.ash-actions{display:flex;gap:6px;flex:0 0 auto;}' +
      '.ash-btn{font-family:inherit;cursor:pointer;border:none;border-radius:7px;padding:6px 10px;' +
      'font-size:11px;font-weight:700;background:rgba(255,255,255,.12);color:var(--paper,#FCFAF6);}' +
      '.ash-btn.ash-logout{background:rgba(193,68,58,.85);}' +
      '.ash-btn:focus-visible{outline:2px solid var(--amber,#E2A93B);outline-offset:2px;}' +
      '.ash-nav{display:flex;gap:6px;flex-wrap:wrap;padding:8px 14px;background:var(--ink,#1B2430);' +
      'border-top:1px solid #333d4d;font-family:inherit;}' +
      '.ash-nav a{color:#8A94A6;text-decoration:none;font-size:12px;font-weight:700;padding:6px 10px;' +
      'border-radius:7px;white-space:nowrap;}' +
      '.ash-nav a:focus-visible{outline:2px solid var(--amber,#E2A93B);outline-offset:2px;}' +
      '.ash-nav a[aria-current="page"]{color:var(--paper,#FCFAF6);background:rgba(255,255,255,.12);' +
      'border-bottom:2px solid var(--amber,#E2A93B);}' +
      '.ash-nav .ash-review-group{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto;' +
      'padding-left:10px;border-left:1px solid #333d4d;align-items:center;}' +
      '.ash-nav .ash-review-label{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:#8A94A6;' +
      'padding:0 2px;}' +
      '@media (max-width:420px){.ash-identity{max-width:32vw;}}';
    document.head.appendChild(style);
  }

  function navLink(item, activeKey) {
    var isActive = item.key === activeKey;
    var a = document.createElement('a');
    a.href = item.href;
    a.textContent = item.label;
    if (isActive) a.setAttribute('aria-current', 'page');
    return a;
  }

  // ---------- reporting nav (Dashboard/Daily Figures/History/[Operations]/[Admin]) ----------
  function reportingNavItems(role, flags) {
    var items = [];
    if (enabled(flags, 'dashboard')) items.push({ key: 'dashboard', label: 'Dashboard', href: '/dashboard.html' });
    if ((role === 'manager' || role === 'super_admin') && enabled(flags, 'dispatch')) {
      items.push({ key: 'operations', label: 'Operations', href: '/dispatch.html' });
    }
    if (enabled(flags, 'daily_figures')) items.push({ key: 'daily_figures', label: 'Daily Figures', href: '/' });
    if (enabled(flags, 'history_exports')) items.push({ key: 'history_exports', label: 'History & Exports', href: '/history.html' });
    if (role === 'super_admin') items.push({ key: 'admin', label: 'Admin', href: '/admin.html' });
    return items;
  }

  // ---------- operational switcher (Dispatch/Returns/Production) ----------
  function operationalNavItems(flags) {
    var items = [];
    if (enabled(flags, 'dispatch')) items.push({ key: 'dispatch', label: 'Dispatch', href: '/dispatch.html' });
    if (enabled(flags, 'returns')) items.push({ key: 'returns', label: 'Returns', href: '/returns.html' });
    if (enabled(flags, 'production')) items.push({ key: 'production', label: 'Production', href: '/production.html' });
    return items;
  }

  function reviewLinkItems(flags) {
    var items = [];
    if (enabled(flags, 'daily_figures')) items.push({ key: 'daily_figures', label: 'Daily Figures', href: '/' });
    if (enabled(flags, 'history_exports')) items.push({ key: 'history_exports', label: 'History & Exports', href: '/history.html' });
    return items;
  }

  function renderNav(container, role, flags, pageKey) {
    container.innerHTML = '';
    container.setAttribute('aria-label', 'Primary');
    var nav = document.createElement('nav');
    nav.className = 'ash-nav';

    if (role === 'operator') {
      // Operators get a single, unchanging navigation frame everywhere
      // they can reach: the three data-entry books, plus review-only
      // links to Daily Figures/History & Exports, visually separated
      // rather than mixed into one row.
      operationalNavItems(flags).forEach(function (item) {
        nav.appendChild(navLink(item, pageKey));
      });
      var review = reviewLinkItems(flags);
      if (review.length) {
        var group = document.createElement('span');
        group.className = 'ash-review-group';
        var label = document.createElement('span');
        label.className = 'ash-review-label';
        label.textContent = 'Review';
        group.appendChild(label);
        review.forEach(function (item) { group.appendChild(navLink(item, pageKey)); });
        nav.appendChild(group);
      }
    } else if (OPERATIONAL_PAGES.indexOf(pageKey) !== -1) {
      // Manager/Super Admin/Viewer, while actually on an operational book
      // page: the focused three-item switcher only — Home/Back is the
      // route back to the reporting area, not a crowded combined row.
      operationalNavItems(flags).forEach(function (item) {
        nav.appendChild(navLink(item, pageKey));
      });
    } else {
      reportingNavItems(role, flags).forEach(function (item) {
        nav.appendChild(navLink(item, pageKey));
      });
    }

    if (nav.children.length) container.appendChild(nav);
  }

  function renderIdentityBar(container, user, homeHref) {
    container.innerHTML = '';
    var bar = document.createElement('div');
    bar.className = 'ash-bar';
    bar.setAttribute('role', 'banner');

    var brand = document.createElement('div');
    brand.className = 'ash-brand';
    var logo = document.createElement('img');
    logo.setAttribute('data-brand-logo', '');
    logo.className = 'hidden';
    logo.alt = '';
    var name = document.createElement('span');
    name.setAttribute('data-brand-name', '');
    brand.appendChild(logo);
    brand.appendChild(name);

    var identity = document.createElement('span');
    identity.className = 'ash-identity';
    var roleLabel = ROLE_LABELS[user.role] || user.role;
    identity.innerHTML = escapeHtml(user.username) + ' <span class="ash-role">· ' + escapeHtml(roleLabel) + '</span>';

    var actions = document.createElement('div');
    actions.className = 'ash-actions';

    var backBtn = document.createElement('button');
    backBtn.type = 'button';
    backBtn.className = 'ash-btn';
    backBtn.textContent = 'Back';
    backBtn.addEventListener('click', function () { goBack(homeHref); });

    var homeBtn = document.createElement('button');
    homeBtn.type = 'button';
    homeBtn.className = 'ash-btn';
    homeBtn.textContent = 'Home';
    homeBtn.addEventListener('click', function () { location.href = homeHref; });

    var logoutBtn = document.createElement('button');
    logoutBtn.type = 'button';
    logoutBtn.className = 'ash-btn ash-logout';
    logoutBtn.textContent = 'Log out';
    logoutBtn.addEventListener('click', async function () {
      try { await fetch('/api/logout', { method: 'POST' }); } catch (e) { /* still redirect below either way */ }
      location.href = '/';
    });

    actions.appendChild(backBtn);
    actions.appendChild(homeBtn);
    actions.appendChild(logoutBtn);

    bar.appendChild(brand);
    bar.appendChild(identity);
    bar.appendChild(actions);
    container.appendChild(bar);
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  async function apiGet(path) {
    var res = await fetch(path);
    if (res.status === 401) return null;
    return res.json().catch(function () { return null; });
  }

  var AppShell = {
    resolveLanding: resolveLanding,
    currentPageKey: currentPageKey,
    user: null,
    flags: {},
  };

  async function render() {
    var identityContainer = document.getElementById('appIdentityBar');
    var navContainer = document.getElementById('appRoleNav');
    if (!identityContainer && !navContainer) return; // page doesn't opt into the shared shell

    var session = await apiGet('/api/session');
    if (!session || !session.authed || !session.user) {
      AppShell.user = null;
      return; // login screen or an expired session — nothing to render
    }
    AppShell.user = session.user;

    var flagsData = await apiGet('/api/feature-flags');
    var flags = {};
    (Array.isArray(flagsData) ? flagsData : []).forEach(function (f) { flags[f.module_key] = f.enabled; });
    AppShell.flags = flags;

    var homeHref = resolveLanding(session.user.role, flags);
    var pageKey = currentPageKey();

    if (identityContainer) renderIdentityBar(identityContainer, session.user, homeHref);
    if (navContainer) renderNav(navContainer, session.user.role, flags, pageKey);

    // The page's own applyBranding() (already defined identically on every
    // page) already targets every [data-brand-name]/[data-brand-logo]
    // element in the document — calling it again now that the identity
    // bar's own copies of those exist is enough to brand them too, with
    // zero branding-fetch logic duplicated here.
    if (typeof window.applyBranding === 'function') {
      try { window.applyBranding(); } catch (e) { /* branding is cosmetic */ }
    }

    pushBreadcrumb();
  }

  AppShell.refresh = render;
  window.AppShell = AppShell;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
