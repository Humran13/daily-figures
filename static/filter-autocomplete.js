(function () {
  'use strict';

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char];
    });
  }

  function create(options) {
    var input = document.getElementById(options.inputId);
    var results = document.getElementById(options.resultsId);
    var timer = null;
    var requestToken = 0;
    var items = [];
    var activeIndex = -1;

    function hide() {
      results.classList.add('hidden');
      activeIndex = -1;
    }

    function highlight() {
      results.querySelectorAll('.autocomplete-item').forEach(function (element, index) {
        element.classList.toggle('active', index === activeIndex);
      });
    }

    function select(item) {
      input.value = item.name;
      input.dataset.selectedId = String(item.id);
      hide();
      if (options.onChange) options.onChange();
    }

    function render(nextItems) {
      items = nextItems;
      activeIndex = -1;
      results.innerHTML = items.length
        ? items.map(function (item, index) {
            return '<div class="autocomplete-item" role="option" data-index="' + index + '">' +
              escapeHtml(item.name) + '</div>';
          }).join('')
        : '<div class="autocomplete-item" aria-disabled="true">No matches</div>';
      results.classList.remove('hidden');
      results.querySelectorAll('[data-index]').forEach(function (element) {
        element.addEventListener('mousedown', function (event) { event.preventDefault(); });
        element.addEventListener('click', function () { select(items[Number(element.dataset.index)]); });
      });
    }

    async function search() {
      var query = input.value.trim();
      var token = ++requestToken;
      if (query.length < 1) {
        hide();
        return;
      }
      var found = await options.source(query);
      if (token !== requestToken || input.value.trim() !== query) return;
      render(Array.isArray(found) ? found : []);
    }

    input.addEventListener('input', function () {
      input.dataset.selectedId = '';
      clearTimeout(timer);
      if (options.onChange) options.onChange();
      timer = setTimeout(search, 150);
    });
    input.addEventListener('keydown', function (event) {
      if (results.classList.contains('hidden')) return;
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        activeIndex = Math.min(activeIndex + 1, items.length - 1);
        highlight();
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        activeIndex = Math.max(activeIndex - 1, 0);
        highlight();
      } else if (event.key === 'Enter' && activeIndex >= 0) {
        event.preventDefault();
        select(items[activeIndex]);
      } else if (event.key === 'Escape') {
        hide();
      }
    });
    input.addEventListener('focus', function () {
      if (input.value.trim().length >= 1) search();
    });
    document.addEventListener('click', function (event) {
      if (!event.target.closest('#' + options.inputId) && !event.target.closest('#' + options.resultsId)) hide();
    });

    return {
      clear: function () {
        input.value = '';
        input.dataset.selectedId = '';
        hide();
      },
      set: function (id, name) {
        input.value = name || '';
        input.dataset.selectedId = id == null ? '' : String(id);
        hide();
      },
      selectedId: function () {
        return input.dataset.selectedId || '';
      },
    };
  }

  window.FilterAutocomplete = { create: create };
})();
