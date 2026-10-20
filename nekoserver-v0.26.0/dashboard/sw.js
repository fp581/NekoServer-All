'use strict';
/* Lets the dashboard open like an app and still show its page when the server can't
 * be reached (it then says E01). Readings (/api/...) are never stored. */
const CACHE = 'neko-shell-v1';
const SHELL = ['/', '/style.css', '/app.js', '/logo.svg', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png', '/apple-touch-icon.png'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  // The server first, so an update shows at once; the saved copy only when offline.
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok && SHELL.includes(url.pathname)) {
      const copy = response.clone();
      event.waitUntil(caches.open(CACHE).then(cache => cache.put(event.request, copy)).catch(() => undefined));
    }
    return response;
  }).catch(() => caches.match(event.request).then(hit => hit || caches.match('/'))));
});
