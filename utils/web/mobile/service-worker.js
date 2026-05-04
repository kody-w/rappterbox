// service-worker.js — local-first app shell cache.
//
// Strategy:
//   - On install: precache the app shell so the app boots offline forever.
//   - On fetch: serve cached shell instantly, hit network only for /api calls
//     (the user's chosen LLM endpoint, T2T peer URLs, etc.).
//   - Network is OPTIONAL. The app is fully functional with no connection
//     EXCEPT for the LLM call itself (and even that's pluggable to a local
//     model via WebLLM in v2).

const CACHE_VERSION = 'twin-v1';
const APP_SHELL = [
  './',
  './index.html',
  './rapp-mobile.js',
  './manifest.webmanifest',
  './icon-192.svg',
  './icon-512.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never cache API calls — LLM, peer T2T, etc. need fresh network.
  if (url.pathname.startsWith('/api/') ||
      url.hostname.endsWith('.openai.azure.com') ||
      url.hostname === 'api.openai.com' ||
      url.hostname === 'api.anthropic.com') {
    return;  // pass through to network
  }

  // App shell: cache-first, fall back to network, fall back to offline page.
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).catch(() => {
        // Offline + not cached: at least serve index.html for navigations
        if (event.request.mode === 'navigate') {
          return caches.match('./index.html');
        }
        return new Response('offline', { status: 503 });
      });
    })
  );
});
