/*
 * Shared Spare Parts search for every picker and search box (Stock In,
 * Stock Out, Adjustments, Master Data, History, Admin Pricing). It only
 * calls GET /api/spare-parts?q=..., so the matching rules live in ONE
 * place on the server (webapp/services/spare_part_service.py's
 * search_spare_parts()). Name, specifications, model, size, code and linked
 * machines all match, case-insensitively, so "Bearing" and "22213" find the
 * same item.
 */
(function () {
  'use strict';

  var MAX_RESULTS = 50;

  (function injectStyle() {
    if (document.getElementById('spareSearchStyle')) return;
    var style = document.createElement('style');
    style.id = 'spareSearchStyle';
    style.textContent = '.autocomplete-meta{font-size:11px;color:#3D4756;margin-top:2px;}';
    document.head.appendChild(style);
  })();

  // "Bearing — 22213" (name first, then specifications when present).
  function label(part) {
    return part.specifications ? part.name + ' — ' + part.specifications : part.name;
  }

  // Secondary line in a results list: current stock, shown separately.
  function meta(part) {
    return 'Stock: ' + part.current_stock + ' ' + part.unit;
  }

  async function search(query, options) {
    var params = new URLSearchParams();
    params.set('q', query || '');
    if (options && options.includeInactive) params.set('include_inactive', '1');
    var res = await fetch('/api/spare-parts?' + params.toString());
    if (!res.ok) return [];
    var parts = await res.json();
    return parts.slice(0, MAX_RESULTS);
  }

  // Ready-made source for FilterAutocomplete.create({ source, label, meta }).
  function source(options) {
    return function (query) { return search(query, options); };
  }

  window.SparePartSearch = {
    label: label,
    meta: meta,
    search: search,
    source: source,
  };
})();
