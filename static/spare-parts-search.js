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

  // Shared presentation identity supplied by the API. The fallback keeps
  // this compatible with older cached responses during a rolling deploy.
  function label(part) {
    if (part.display_name) return part.display_name;
    var specification = String(part.specifications == null ? '' : part.specifications).trim();
    return specification ? part.name + ' [' + specification + ']' : part.name;
  }

  // Secondary line in a results list: current stock, shown separately.
  function meta(part) {
    var quantity = window.SparePartsUI ? SparePartsUI.formatQuantity(part.current_stock) : part.current_stock;
    return 'Stock: ' + quantity + ' ' + part.unit;
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
