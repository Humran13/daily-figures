/*
 * Minimal, conservative service worker for the installed PWA shell only.
 * This app is authenticated, role-gated, and its stock data (Dispatch,
 * Returns, Production, Daily Figures, customers, users, branding settings,
 * reports) changes constantly — none of that is ever cached here. Only a
 * handful of small, stable, unauthenticated static assets needed to make
 * the installed app shell feel instant are cached, and even those are
 * always network-refreshed on every new service worker version.
 */
const CACHE_VERSION = 'df-shell-v2';
// "/manifest.webmanifest" is deliberately NOT precached here as of Stage 6 —
// it's now served dynamically (see webapp/routes/pwa.py) and reflects the
// current company logo/icons, so it must always be fetched fresh rather
// than risk the service worker getting permanently stuck on an old logo.
// The generic fallback icons stay precached since they're genuinely static.
const SHELL_ASSETS = [
  '/pwa.js',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-maskable-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .catch(() => {})
  );
  // Take over as soon as this version is installed, rather than staying
  // "waiting" until every open tab closes — an update must never leave
  // users stuck on a stale shell indefinitely.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((name) => name !== CACHE_VERSION).map((name) => caches.delete(name))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // never intercept writes

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // never touch cross-origin requests

  // Every authenticated/business-data route lives under /api/ — none of it
  // is ever cached, so no user can ever be served another session's data
  // and stock figures are always live.
  if (url.pathname.startsWith('/api/')) return;

  // HTML page navigations always go straight to the network — never
  // intercepted, never cached — so an app update or a permission/role
  // change is never masked by a stale cached page.
  if (req.mode === 'navigate') return;

  // Only the small precached shell assets are served cache-first; every
  // other static file passes straight through to the network untouched.
  if (SHELL_ASSETS.includes(url.pathname)) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req))
    );
  }
});

// ---------- Web Push (correction-request notifications) ----------
// Entirely optional — this handler only ever fires for a device that
// explicitly opted in (see static/requests.html's initPushOptIn()) and a
// deployment with VAPID configured (webapp/services/push_service.py);
// neither of those is assumed here, this file just reacts if/when a
// push message actually arrives. The payload is always the small JSON
// shape webapp/services/push_service.py sends: {title, body, url} — no
// sensitive business figures, matching "never expose sensitive
// information in lock-screen push notifications unnecessarily".
// Background app-icon badging — feature-detected: the Badging API's
// setAppBadge()/clearAppBadge() are only reachable from a Service Worker
// on platforms that implement the (newer, less consistently supported)
// WorkerNavigator extension of the spec. Never assumed available; never
// throws on a platform that lacks it (Safari/iOS in particular). The
// in-app red Requests badge (static/app-shell.js's setAppIconBadge(),
// only reachable once the app is actually opened) remains the
// guaranteed fallback regardless of what this can or can't do.
function updateBackgroundBadge(count) {
  try {
    if (!self.navigator || !('setAppBadge' in self.navigator)) return;
    if (typeof count === 'number' && count > 0) {
      self.navigator.setAppBadge(count).catch(() => {});
    } else if ('clearAppBadge' in self.navigator) {
      self.navigator.clearAppBadge().catch(() => {});
    }
  } catch (e) { /* unsupported platform — never fatal */ }
}

self.addEventListener('push', (event) => {
  let payload = { title: 'Daily Figures', body: 'You have a new notification.', url: '/requests.html' };
  try {
    if (event.data) payload = { ...payload, ...event.data.json() };
  } catch (e) { /* malformed/empty payload — fall back to the generic message above */ }

  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: '/icons/icon-192.png',
      badge: '/icons/icon-192.png',
      data: { url: payload.url || '/requests.html' },
    }).catch(() => {})
  );
  // Keeps the app icon's badge accurate even while the PWA is fully
  // closed/backgrounded, on platforms that support it — see
  // updateBackgroundBadge() above. badgeCount is only ever present on
  // the "new correction request" notification (see webapp/services/
  // notification_service.py); other notification types simply omit it
  // and this is a no-op.
  if (typeof payload.badgeCount === 'number') {
    event.waitUntil(Promise.resolve(updateBackgroundBadge(payload.badgeCount)));
  }
});

// Clicking the notification focuses an already-open Requests tab if one
// exists, or opens a new one — never silently does nothing.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/requests.html';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if (client.url.includes('/requests.html') && 'focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(targetUrl);
    })
  );
});
