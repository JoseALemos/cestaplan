// CestaPlan — service worker for the offline app shell.
//
// Scope: cache the static shell (landing + core navigation chrome) so the
// PWA opens instantly and gracefully offline. Data screens (plan, grocery
// list) will layer their own IndexedDB-backed offline strategy on top of
// this once the API contract lands — this file only owns the shell cache.
//
// Navigations use a NETWORK-FIRST strategy: a cache-first HTML shell goes
// stale on every rebuild/deploy and keeps referencing hashed JS/CSS chunks
// that no longer exist, which breaks the page ("This page couldn't load").
// Fresh HTML must always win; the cache is only an offline fallback.
const SHELL_CACHE = "cestaplan-shell-v2";

const SHELL_URLS = ["/", "/manifest.webmanifest", "/icons/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key !== SHELL_CACHE).map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // API calls: always go to the network, never served from the shell cache.
  if (url.pathname.startsWith("/api/")) return;

  // Navigations (HTML documents): network-first. The freshly built HTML — with
  // its current chunk references — always wins; the cached shell is served only
  // when the network is unavailable (offline).
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match(request).then((cached) => cached ?? caches.match("/")),
      ),
    );
    return;
  }

  // Static assets (hashed, immutable): stale-while-revalidate.
  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(SHELL_CACHE).then((cache) => cache.put(request, clone));
          }
          return response;
        })
        .catch(() => cached);
      return cached ?? network;
    }),
  );
});
