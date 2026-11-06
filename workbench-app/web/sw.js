/* Jarvis 工作台 PWA service worker：外壳 network-first，API 永不缓存。
   离线时回退到上一次成功的静态外壳；数据仍来自本机 8787 服务。 */
const CACHE = "jarvis-workbench-shell-v2";
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
    // no-store 让每次导航都绕过浏览器 HTTP 缓存，服务端的 no-cache 头再
    // 保证 ETag 重新验证；离线时才回退到上一次成功的 shell 副本。
    fetch(event.request, { cache: "no-store" })
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy)).catch(() => undefined);
        }
        return response;
      })
      .catch(async () => {
        const hit = await caches.match(event.request);
        if (hit) return hit;
        // Only a navigation may fall back to the cached HTML shell. Returning
        // HTML for a failed script/style/icon request makes the browser parse
        // markup as JS/CSS; non-UI paths like /healthz must fail honestly.
        if (event.request.mode === "navigate") {
          return (await caches.match("/")) || Response.error();
        }
        return Response.error();
      }),
  );
});