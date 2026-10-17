/* Shared presentation and mutation UX for every Spare Parts screen. */
(function () {
  'use strict';

  function formatQuantity(value) {
    if (value == null || value === '') return '—';
    var number = Number(value);
    if (!Number.isFinite(number)) return String(value);
    return number.toLocaleString('en-US', {maximumFractionDigits: 3, useGrouping: false});
  }

  function formatUGX(value, prefix) {
    if (value == null || value === '') return '—';
    var number = Number(value);
    if (!Number.isFinite(number)) return String(value);
    var formatted = number.toLocaleString('en-US', {
      minimumFractionDigits: Number.isInteger(number) ? 0 : 2,
      maximumFractionDigits: 2,
    });
    return (prefix === false ? '' : 'UGX ') + formatted;
  }

  function ensureToastRegion() {
    var region = document.getElementById('sparePartsNotifications');
    if (region) return region;
    region = document.createElement('div');
    region.id = 'sparePartsNotifications';
    region.setAttribute('aria-live', 'polite');
    region.style.cssText = 'position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:1000;width:min(92vw,520px);pointer-events:none';
    document.body.appendChild(region);
    return region;
  }

  function notify(message, type, options) {
    type = type || 'success';
    var toast = document.createElement('div');
    var colors = {success:'#1F8A70', warning:'#9A6410', error:'#C1443A'};
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
    toast.style.cssText = 'background:#fff;border:2px solid ' + (colors[type] || colors.success) +
      ';border-radius:10px;padding:11px 14px;margin-bottom:8px;box-shadow:0 4px 16px rgba(0,0,0,.18);font:600 13px system-ui;color:#1B2430;pointer-events:auto';
    toast.textContent = message;
    ensureToastRegion().appendChild(toast);
    var delay = options && options.persistent ? 0 : (type === 'error' ? 9000 : 4000);
    if (delay) window.setTimeout(function () { toast.remove(); }, delay);
    return toast;
  }

  async function withBusy(button, task, busyLabel) {
    if (!button || button.disabled) return;
    var label = button.textContent;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    if (busyLabel) button.textContent = busyLabel;
    try { return await task(); }
    finally {
      button.disabled = false;
      button.removeAttribute('aria-busy');
      button.textContent = label;
    }
  }

  window.SparePartsUI = {
    formatQuantity: formatQuantity,
    formatUGX: formatUGX,
    notify: notify,
    withBusy: withBusy,
  };
})();
