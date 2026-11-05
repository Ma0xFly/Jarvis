/* Jarvis 工作台 PWA service worker：外壳 network-first，API 永不缓存。
   离线时回退到上一次成功的静态外壳；数据仍来自本机 8787 服务。 */
const CACHE = "jarvis-workbench-shell-v1";
const SHELL = [
  "/",
  "/static/index.js",
  "/static/style.css",
  "/static/theme.css",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .catch(() => undefined)
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (
    event.request.method !== "GET" ||
    url.origin !== self.location.origin ||
    url.pathname.startsWith("/api/")
  ) {
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy)).catch(() => undefined);
        }
        return response;
      })
      .catch(() =>
        caches.match(event.request).then((hit) => hit || caches.match("/")),
      ),
  );
});