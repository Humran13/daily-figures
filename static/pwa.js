/*
 * Shared PWA install UI + service worker registration, loaded identically
 * by every primary page (see each page's <script src="/pwa.js" defer>).
 * This is the one place this logic lives — never duplicated per page.
 *
 * Builds its own DOM (a dismissible install banner, a small persistent
 * "Install App" affordance, and an iOS instruction modal) at runtime so no
 * page's own markup has to change beyond the one <script> tag and the
 * <head> metadata. Styling reuses each page's own CSS custom properties
 * (--ink/--teal/--paper/--amber/--ink-soft), which are identical across
 * every page in this app, with hard-coded fallbacks in case a page is ever
 * missing one.
 */
(function () {
  'use strict';

  var DISMISS_KEY = 'pwaInstallDismissedUntil';
  var DISMISS_DAYS = 7;
  var deferredPrompt = null;

  function isStandalone() {
    return (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) ||
      window.navigator.standalone === true;
  }

  function isIos() {
    return /iphone|ipad|ipod/i.test(navigator.userAgent) && !window.MSStream;
  }

  function isStandardSafari() {
    // Chrome/Firefox/Edge/Opera/in-app browsers on iOS all still report
    // "Safari" in their UA string, so they have to be excluded explicitly —
    // only true Safari lacks all of these extra tokens.
    var ua = navigator.userAgent.toLowerCase();
    var otherBrowserTokens = ['crios', 'fxios', 'edgios', 'opios', 'fban', 'fbav', 'instagram', 'line', 'micromessenger'];
    return /safari/.test(ua) && !otherBrowserTokens.some(function (t) { return ua.indexOf(t) !== -1; });
  }

  function dismissedRecently() {
    var raw;
    try { raw = localStorage.getItem(DISMISS_KEY); } catch (e) { return false; }
    if (!raw) return false;
    var until = parseInt(raw, 10);
    return !isNaN(until) && Date.now() < until;
  }

  function rememberDismissal() {
    try {
      localStorage.setItem(DISMISS_KEY, String(Date.now() + DISMISS_DAYS * 24 * 60 * 60 * 1000));
    } catch (e) { /* private browsing / storage disabled — never block the app over this */ }
  }

  function isTypingInFormField() {
    var active = document.activeElement;
    if (!active) return false;
    var tag = (active.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || active.isContentEditable;
  }

  function injectStyles() {
    var style = document.createElement('style');
    style.textContent =
      '.pwa-install-banner{position:fixed;left:12px;right:12px;bottom:12px;z-index:200;' +
      'background:var(--ink,#1B2430);color:var(--paper,#FCFAF6);border-radius:14px;padding:14px 16px;' +
      'box-shadow:0 6px 24px rgba(0,0,0,.25);display:flex;flex-direction:column;gap:10px;' +
      'max-width:420px;margin:0 auto;font-family:inherit;}' +
      '.pwa-install-banner.hidden{display:none;}' +
      '.pwa-install-banner p{margin:0;font-size:13px;line-height:1.4;}' +
      '.pwa-install-actions{display:flex;gap:10px;}' +
      '.pwa-install-actions button{flex:1;font-family:inherit;cursor:pointer;border:none;border-radius:8px;' +
      'padding:10px;font-size:13px;font-weight:700;}' +
      '#pwaInstallBtn{background:var(--teal,#1F8A70);color:#fff;}' +
      '#pwaInstallDismissBtn{background:rgba(255,255,255,.14);color:var(--paper,#FCFAF6);}' +
      '.pwa-install-persistent{position:fixed;right:14px;bottom:14px;z-index:190;' +
      'background:var(--teal,#1F8A70);color:#fff;border:none;border-radius:24px;padding:10px 16px;' +
      'font-size:12px;font-weight:700;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.2);font-family:inherit;}' +
      '.pwa-install-persistent.hidden{display:none;}' +
      '.pwa-ios-modal{position:fixed;inset:0;background:rgba(27,36,48,.6);z-index:210;display:flex;' +
      'align-items:center;justify-content:center;padding:20px;}' +
      '.pwa-ios-modal.hidden{display:none;}' +
      '.pwa-ios-modal-sheet{background:var(--paper,#FCFAF6);color:var(--ink,#1B2430);border-radius:16px;' +
      'padding:22px 20px;max-width:340px;width:100%;position:relative;font-family:inherit;}' +
      '.pwa-ios-modal-sheet h3{margin:0 0 12px;font-size:16px;}' +
      '.pwa-ios-modal-sheet ol{margin:0 0 14px;padding-left:20px;font-size:13px;line-height:1.6;}' +
      '.pwa-ios-note{font-size:12px;color:var(--ink-soft,#3D4756);margin:0 0 14px;}' +
      '#pwaIosModalClose{position:absolute;top:8px;right:10px;background:none;border:none;font-size:20px;' +
      'cursor:pointer;color:var(--ink-soft,#3D4756);padding:6px 10px;line-height:1;}' +
      '#pwaInstallBtn:focus-visible,#pwaInstallDismissBtn:focus-visible,' +
      '.pwa-install-persistent:focus-visible,#pwaIosModalClose:focus-visible{' +
      'outline:2px solid var(--amber,#E2A93B);outline-offset:2px;}';
    document.head.appendChild(style);
  }

  function buildDom() {
    var banner = document.createElement('div');
    banner.id = 'pwaInstallBanner';
    banner.className = 'pwa-install-banner hidden';
    banner.setAttribute('role', 'dialog');
    banner.setAttribute('aria-live', 'polite');
    banner.innerHTML =
      '<p id="pwaInstallText">Install this app on your phone for quicker access.</p>' +
      '<div class="pwa-install-actions">' +
      '<button type="button" id="pwaInstallBtn">Install App</button>' +
      '<button type="button" id="pwaInstallDismissBtn">Not now</button>' +
      '</div>';

    var persistentBtn = document.createElement('button');
    persistentBtn.type = 'button';
    persistentBtn.id = 'pwaInstallPersistentBtn';
    persistentBtn.className = 'pwa-install-persistent hidden';
    persistentBtn.setAttribute('aria-label', 'Install App');
    persistentBtn.textContent = 'Install App';

    var iosModal = document.createElement('div');
    iosModal.id = 'pwaIosModal';
    iosModal.className = 'pwa-ios-modal hidden';
    iosModal.setAttribute('role', 'dialog');
    iosModal.setAttribute('aria-modal', 'true');
    iosModal.setAttribute('aria-labelledby', 'pwaIosModalTitle');
    iosModal.innerHTML =
      '<div class="pwa-ios-modal-sheet">' +
      '<button type="button" id="pwaIosModalClose" aria-label="Close">&times;</button>' +
      '<h3 id="pwaIosModalTitle">Install this app</h3>' +
      '<ol>' +
      '<li>Open this website in Safari.</li>' +
      '<li>Tap the Share button.</li>' +
      '<li>Tap &ldquo;Add to Home Screen.&rdquo;</li>' +
      '<li>Confirm by tapping &ldquo;Add.&rdquo;</li>' +
      '</ol>' +
      '<p class="pwa-ios-note" id="pwaIosNote"></p>' +
      '</div>';

    document.body.appendChild(banner);
    document.body.appendChild(persistentBtn);
    document.body.appendChild(iosModal);
    return { banner: banner, persistentBtn: persistentBtn, iosModal: iosModal };
  }

  async function applyBrandingToText(el) {
    var fallback = 'Install this app on your phone for quicker access.';
    try {
      var res = await fetch('/api/branding');
      var b = await res.json();
      var name = (b && b.display_name) ? b.display_name : null;
      el.textContent = name ? ('Install ' + name + ' on your phone for quicker access.') : fallback;
    } catch (e) {
      el.textContent = fallback; // branding is cosmetic — never block the install UI over a failed fetch
    }
  }

  function init() {
    if (isStandalone()) return; // already installed and running as the app — never show install UI

    injectStyles();
    var dom = buildDom();

    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(function () { /* app must work fine without it */ });
    }

    function showBanner() {
      if (isTypingInFormField()) { setTimeout(showBanner, 1500); return; }
      dom.banner.classList.remove('hidden');
      dom.persistentBtn.classList.add('hidden');
    }
    function showPersistentOnly() {
      dom.banner.classList.add('hidden');
      dom.persistentBtn.classList.remove('hidden');
    }
    function hideAllInstallUi() {
      dom.banner.classList.add('hidden');
      dom.persistentBtn.classList.add('hidden');
    }

    function reveal() {
      applyBrandingToText(document.getElementById('pwaInstallText'));
      if (dismissedRecently()) showPersistentOnly();
      else showBanner();
    }

    window.addEventListener('beforeinstallprompt', function (e) {
      e.preventDefault(); // suppress the browser's own mini-infobar — our UI replaces it
      deferredPrompt = e;
      reveal();
    });

    window.addEventListener('appinstalled', function () {
      deferredPrompt = null;
      hideAllInstallUi();
      try { localStorage.setItem('pwaInstalled', '1'); } catch (e) {}
    });

    if (isIos()) {
      // iOS never fires beforeinstallprompt at all — the custom UI is the
      // only path to "Add to Home Screen" instructions, so it's revealed
      // immediately rather than waiting for an event that will never come.
      reveal();
    }

    function closeIosModal() { dom.iosModal.classList.add('hidden'); }

    function openIosModal() {
      var note = document.getElementById('pwaIosNote');
      note.textContent = isStandardSafari() ? '' :
        'This looks like a different in-app or third-party browser — Safari may be required for "Add to Home Screen" to appear.';
      dom.iosModal.classList.remove('hidden');
    }

    async function attemptInstall() {
      if (deferredPrompt) {
        var prompted = deferredPrompt;
        deferredPrompt = null;
        dom.banner.classList.add('hidden');
        dom.persistentBtn.classList.add('hidden');
        try {
          prompted.prompt();
          var choice = await prompted.userChoice;
          if (!choice || choice.outcome !== 'accepted') {
            // Dismissed the native OS prompt — appinstalled will not fire;
            // keep the persistent affordance available without re-nagging
            // with the full banner. Never claim success here.
            showPersistentOnly();
          }
          // On acceptance, the 'appinstalled' listener above is the only
          // thing allowed to declare success and hide the UI for good.
        } catch (e) {
          showPersistentOnly();
        }
        return;
      }
      if (isIos()) { openIosModal(); return; }
      // Neither an available deferred prompt nor iOS — nothing meaningful
      // to do, and never show a fake success message.
    }

    document.getElementById('pwaInstallBtn').addEventListener('click', attemptInstall);
    dom.persistentBtn.addEventListener('click', attemptInstall);
    document.getElementById('pwaInstallDismissBtn').addEventListener('click', function () {
      rememberDismissal();
      showPersistentOnly();
    });
    document.getElementById('pwaIosModalClose').addEventListener('click', closeIosModal);
    dom.iosModal.addEventListener('click', function (e) { if (e.target === dom.iosModal) closeIosModal(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !dom.iosModal.classList.contains('hidden')) closeIosModal();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
