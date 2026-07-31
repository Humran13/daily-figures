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
