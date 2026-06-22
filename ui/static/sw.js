// Network-first-for-navigation service worker.
//
// Why not pure pass-through: iOS standalone (home-screen) PWAs aggressively cache
// the app-shell HTML and don't reliably revalidate it on relaunch, so new
// ?v=<n> asset references never load and UI updates appear "stuck". Here we force
// every navigation (the HTML) to bypass the HTTP cache (`cache: 'reload'`), which
// pulls the latest index.html -> latest versioned app.js/styles.css. Versioned
// assets cache-bust themselves via their query string, and API calls are left to
// default behavior (never SW-cached). Falls back to the cached shell only offline.
const SHELL = 'argus-shell-v1';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || req.mode !== 'navigate') return;  // only the app shell
  e.respondWith(
    fetch(req, { cache: 'reload' })
      .then(res => {
        const copy = res.clone();
        caches.open(SHELL).then(c => c.put(req, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(req))  // offline fallback
  );
});
