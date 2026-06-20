// Pass-through service worker — registered for PWA eligibility and offline
// hooks only. Do not cache, so updates pick up immediately on reload.
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => self.clients.claim());
self.addEventListener('fetch', () => {});
