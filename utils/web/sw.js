// rapp_brainstem/web/sw.js — service worker for the Brainstem desktop PWA.
//
// Strategy:
//   - Precache the static UI shell on install.
//   - Never cache /chat or /api/* — those need fresh server responses.
//   - For everything else under same-origin: cache-first with network fallback,
//     so the UI loads instantly even with flaky connectivity. The brainstem
//     itself still needs to be running on :7071 for chat to work; the SW only
//     promises that the *UI shell* is offline-resilient.

const CACHE_VERSION = 'rapp-brainstem-v2';
const SHELL = [
  './',
  './index.html',
  './rapp.js',
  './manifest.webmanifest',
  './icon-180.png',
  './icon-192.png',
  './icon-512.png',
  './icon-192.svg',
  './icon-512.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) =>
      Promise.all(SHELL.map((url) =>
        cache.add(url).catch((err) => console.warn('precache miss', url, err))
      ))
    )
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

  // Never cache the live chat surface or any API call.
  if (url.pathname.startsWith('/chat') ||
      url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/login') ||
      url.pathname.startsWith('/agents/files') ||
      url.pathname.startsWith('/voice/') ||
      url.pathname.startsWith('/twin/') ||
      url.pathname.startsWith('/models/')) {
    return;
  }

  // Same-origin shell assets: cache-first with network fallback.
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).catch(() => {
          if (event.request.mode === 'navigate') return caches.match('./index.html');
          return new Response('offline', { status: 503 });
        });
      })
    );
  }
});

// ── PWA push (Phase 2 — wired up but inert until VAPID server lands) ──
// iOS 16.4+ delivers Web Push to installed PWAs. The SW receives the
// push event even when no tab is open, fires a notification, and on
// notificationclick brings the PWA forward (or opens it). Server-side
// push delivery (VAPID-signed POST to the user's push endpoint) is the
// next surface to grow on the rapp-auth Cloudflare worker.

self.addEventListener('push', (event) => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; }
  catch { payload = { title: 'Brainstem', body: event.data ? event.data.text() : '' }; }
  const title = payload.title || 'Brainstem';
  const body  = payload.body  || '';
  const opts  = {
    body,
    icon: payload.icon || './icon-192.png',
    badge: payload.badge || './icon-192.png',
    tag: payload.tag || 'brainstem-notification',
    data: payload.data || {},
    // iOS respects vibrate when notification is delivered to the app icon
    vibrate: payload.vibrate || [60, 30, 60],
    requireInteraction: !!payload.requireInteraction,
  };
  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || './';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientsList) => {
      for (const c of clientsList) {
        if ('focus' in c) {
          // Bring the PWA window forward and post the payload so the page
          // can route to whatever the notification was about (deep link).
          c.postMessage({ type: 'rapp:notification-click', data: event.notification.data || {} });
          return c.focus();
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })
  );
});

self.addEventListener('pushsubscriptionchange', (event) => {
  // The browser rotated the push subscription. When the rapp-auth
  // worker grows a /api/push/subscribe endpoint, this is where we'd
  // re-register with the new endpoint + p256dh + auth.
  console.log('[sw] push subscription changed (server registration pending)');
});
