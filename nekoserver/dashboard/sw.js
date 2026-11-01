'use strict';
/* Lets the dashboard open like an app and still show its page when the server can't
 * be reached (it then says E01). Readings (/api/...) are never stored. */
const CACHE = 'neko-shell-v1';
const SHELL = [
  '/',
  '/style.css',
  '/app.js',
  '/logo.svg',
  '/manifest.webmanifest',
  '/icon-192.png',
  '/icon-512.png',
  '/apple-touch-icon.png'
];
self.addEventListener('install', event => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then(cache => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});
self.addEventListener('activate', event => {
  event.waitUntil(
    caches
      .keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
async function keep(request, response) {
  try {
    const cache = await caches.open(CACHE);
    await cache.put(request, response);
  } catch {
    /* a copy that cannot be saved is no reason to fail the page */
  }
}
async function saved(request) {
  const hit = await caches.match(request);
  return hit || caches.match('/');
}
// The server first, so an update shows at once; the saved copy only when offline.
async function serve(event, url) {
  try {
    const response = await fetch(event.request);
    if (response.ok && SHELL.includes(url.pathname)) {
      event.waitUntil(keep(event.request, response.clone()));
    }
    return response;
  } catch {
    return saved(event.request);
  }
}
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  event.respondWith(serve(event, url));
});
