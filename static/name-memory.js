/*
 * Small reusable "memory" autocomplete helper for free-text fields that
 * should suggest previously-used values (spare-part Name, Stock Count
 * Reason) without ever selecting an existing database record. Unlike
 * FilterAutocomplete (built around picking one ID-backed row), this only
 * ever fills the text itself — selecting a suggestion never touches any
 * other field.
 *
 * Dedup is case/whitespace-insensitive; the first-seen spelling is kept
 * as the display value. Ranking: exact match, then starts-with, then any
 * word starting with the query, then plain substring.
 */
(function () {
  'use strict';

  function normalizeDisplay(value) {
    return String(value == null ? '' : value).replace(/\s+/g, ' ').trim();
  }

  function escapeRegExp(value) {
    return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function dedupe(rawValues) {
    var byKey = {};
    var order = [];
    (rawValues || []).forEach(function (raw) {
      var display = normalizeDisplay(raw);
      if (!display) return;
      var key = display.toLowerCase();
      if (!byKey[key]) {
        byKey[key] = { id: display, name: display, count: 0 };
        order.push(key);
      }
      byKey[key].count += 1;
    });
    return order.map(function (key) { return byKey[key]; });
  }

  function rank(entries, query, limit) {
    var q = normalizeDisplay(query).toLowerCase();
    if (!q) return [];
    var wordStart = new RegExp('\\b' + escapeRegExp(q));
    var scored = [];
    entries.forEach(function (entry) {
      var name = entry.name.toLowerCase();
      var score;
      if (name === q) score = 0;
      else if (name.indexOf(q) === 0) score = 1;
      else if (wordStart.test(name)) score = 2;
      else if (name.indexOf(q) !== -1) score = 3;
      else return;
      scored.push({ entry: entry, score: score });
    });
    scored.sort(function (a, b) {
      if (a.score !== b.score) return a.score - b.score;
      if (a.entry.name.length !== b.entry.name.length) return a.entry.name.length - b.entry.name.length;
      return a.entry.name.localeCompare(b.entry.name);
    });
    return scored.slice(0, limit || 10).map(function (s) { return s.entry; });
  }

  // A tiny stateful cache: holds a deduped entry list that can be
  // replaced wholesale (setValues) or grown by one new value (addValue)
  // without a refetch, then queried (search).
  function createMemory(options) {
    var limit = (options && options.limit) || 10;
    var entries = [];

    return {
      setValues: function (rawValues) { entries = dedupe(rawValues); },
      addValue: function (raw) {
        var display = normalizeDisplay(raw);
        if (!display) return;
        var key = display.toLowerCase();
        var existing = entries.filter(function (e) { return e.name.toLowerCase() === key; })[0];
        if (existing) existing.count += 1;
        else entries.push({ id: display, name: display, count: 1 });
      },
      search: function (query) { return rank(entries, query, limit); },
    };
  }

  window.NameMemory = { dedupe: dedupe, rank: rank, createMemory: createMemory };
})();
