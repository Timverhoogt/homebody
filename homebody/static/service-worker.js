const CACHE_NAME = "homebody-shell-v119";
const APP_SHELL = [
  "/",
  "/manifest.webmanifest",
  "/static/style.css?v=119",
  "/static/scene.css?v=119",
  "/static/scene.js?v=119",
  "/static/motion.js?v=119",
  "/static/gstwebrtc-api.js",
  "/static/camera.js?v=119",
  "/static/companion.js?v=119",
  "/static/main.js?v=119",
  "/static/ispy-input.js?v=119",
  "/static/shell.css?v=119",
  "/static/fable.css?v=119",
  "/static/workspace-thread.js?v=119",
  "/static/workspace.js?v=119",
  "/static/native-workspace.js?v=119",
  "/static/agent-connection.js?v=119",
  "/static/owner.js?v=119",
  "/static/onboarding.js?v=119",
  "/static/onboarding.css?v=119",
  "/static/notifications.js?v=119",
  "/static/updates.js?v=119",
  "/static/volume.js?v=119",
  "/static/assistant-modes.js?v=119",
  "/static/household.js?v=119",
  "/static/settings-layout.js?v=119",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/icon-maskable-512.png",
  "/static/InstrumentSerif-Regular.woff2",
  "/static/InstrumentSerif-Italic.woff2",
  "/static/DMMono-Regular.woff2",
  "/static/DMMono-Medium.woff2",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)),
    )),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          // Never let a 502 during an app restart become the offline shell.
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put("/", copy));
          }
          return response;
        })
        .catch(() => caches.match("/")),
    );
    return;
  }

  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      })),
    );
  }
});
