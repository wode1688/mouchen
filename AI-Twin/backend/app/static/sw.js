"use strict";

const CACHE_NAME = "mouchen-static-shell-v1";
const STATIC_SHELL = Object.freeze([
  "/static/index.html",
  "/static/app.css",
  "/static/app.js",
  "/static/icon.svg",
  "/static/manifest-en.webmanifest",
  "/static/manifest.webmanifest",
]);
const STATIC_PATHS = new Set(STATIC_SHELL);

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(names => Promise.all(
      names
        .filter(name => name.startsWith("mouchen-static-shell-") && name !== CACHE_NAME)
        .map(name => caches.delete(name)),
    )),
  );
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);

  // API responses, authenticated requests and user content always bypass CacheStorage.
  if (
    request.method !== "GET"
    || url.origin !== self.location.origin
    || url.pathname === "/v1"
    || url.pathname.startsWith("/v1/")
    || request.headers.has("Authorization")
  ) {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match("/static/index.html")));
    return;
  }

  if (!STATIC_PATHS.has(url.pathname)) return;
  event.respondWith(
    caches.match(request).then(cached => cached || fetch(request)),
  );
});
