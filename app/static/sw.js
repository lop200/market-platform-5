// Service worker for the installed app.
//
// Deliberately minimal. This is a trading dashboard: a cached quote is a wrong
// quote, and a stale one shown confidently is worse than no app at all. So
// nothing under /api is ever cached or replayed — those requests go to the
// network and fail loudly if the network is down.
//
// Only the page shell is kept, and only so a cold launch on a bad connection
// shows the interface with its own "connection lost" states rather than the
// browser's error page.

const CACHE = "marsad-shell-v1";
const SHELL = ["/", "/spx", "/news", "/earnings"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Live data, the price stream, and the lock screen must never be served
  // from a cache, nor recorded into one.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/lock")) return;

  // Network first: a working connection always wins. The cache is the
  // fallback for a launch with no signal, never the preferred answer.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && (request.mode === "navigate" || url.pathname.startsWith("/static/"))) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() =>
        caches.match(request).then((cached) => cached || caches.match("/"))
      )
  );
});
