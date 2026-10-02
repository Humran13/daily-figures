/*
 * Shared Spare Parts identity bar + nav — deliberately separate from
 * static/app-shell.js, which is Finished-Goods-only and not section-aware
 * (see webapp/models/section.py's architecture). Never shows a Finished
 * Goods nav item, and app-shell.js never shows a Spare Parts one — each
 * section renders its own nav from its own script.
 *
 * Mounts into the same #appIdentityBar placeholder every page provides.
 * "Change Section" signs out (newest-login-wins semantics: ending this
 * session cleanly) and returns to "/", which shows the section picker.
 */
(function () {
  'use strict';

  var ROLE_LABELS = {
    super_admin: 'Super Administrator', manager: 'Manager', operator: 'Operator',
    viewer: 'Viewer', accountant: 'Accountant',
  };

  var NAV_ITEMS = [
    { key: 'dashboard', label: 'Dashboard', href: '/spare-parts.html', roles: null },
    { key: 'stock-in', label: 'Stock In', href: '/spare-parts-stock-in.html', roles: ['operator', 'manager', 'super_admin'] },
    { key: 'stock-out', label: 'Stock Out', href: '/spare-parts-stock-out.html', roles: ['operator', 'manager', 'super_admin'] },
    { key: 'history', label: 'History', href: '/spare-parts-history.html', roles: null },
    { key: 'master', label: 'Master Data', href: '/spare-parts-master.html', roles: ['manager', 'super_admin'] },
  ];

  function currentPageKey() {
    var path = location.pathname;
    if (path === '/spare-parts.html') return 'dashboard';
    if (path === '/spare-parts-stock-in.html') return 'stock-in';
    if (path === '/spare-parts-stock-out.html') return 'stock-out';
    if (path === '/spare-parts-history.html') return 'history';
    if (path === '/spare-parts-master.html') return 'master';
    return null;
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function render(user) {
    var bar = document.getElementById('appIdentityBar');
    if (!bar) return;
    if (!user) { bar.innerHTML = ''; return; }
    var pageKey = currentPageKey();
    var navHtml = NAV_ITEMS
      .filter(function (item) { return !item.roles || item.roles.indexOf(user.role) !== -1; })
      .map(function (item) {
        return '<a class="sp-nav-link' + (item.key === pageKey ? ' active' : '') + '" href="' + item.href + '">'
          + escapeHtml(item.label) + '</a>';
      }).join('');
    bar.innerHTML =
      '<div class="sp-identity-bar">' +
      '<div class="sp-identity-row">' +
      '<span class="sp-username">' + escapeHtml(user.username) + '</span>' +
      '<span class="sp-role">' + escapeHtml(ROLE_LABELS[user.role] || user.role) + '</span>' +
      '<button type="button" class="sp-change-section" id="spChangeSectionBtn">Change Section</button>' +
      '</div>' +
      '<nav class="sp-nav">' + navHtml + '</nav>' +
      '</div>';
    var btn = document.getElementById('spChangeSectionBtn');
    if (btn) {
      btn.addEventListener('click', async function () {
        await fetch('/api/logout', { method: 'POST' });
        window.location.href = '/';
      });
    }
  }

  async function init() {
    try {
      var res = await fetch('/api/session');
      var session = await res.json();
      if (!session || !session.authed || session.active_section !== 'spare_parts') {
        window.location.href = '/';
        return;
      }
      render(session.user);
    } catch (e) { /* identity bar is cosmetic; page's own init() still runs */ }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.SparePartsShell = { currentPageKey: currentPageKey };
})();
