const CACHE_NAME = "bha-page-webp-v1.0.0";
const CACHE_PREFIX = "bha-page-webp-";
const MAX_ENTRIES = 50;
const sw = /** @type {ServiceWorkerGlobalScope} */ (/** @type {unknown} */ (self));

/** @param {Request} request */
function isPagePreview(request) {
  if (request.method !== "GET") return false;
  const url = new URL(request.url);
  if (url.pathname.includes("/datasets/vomebook/BHA-Source-Files/resolve/main/archives")
    && /\.webp$/i.test(url.pathname)) return true;
  return url.pathname.startsWith("/api/preview/source-page/");
}

/** @param {Cache} cache */
async function trimCache(cache) {
  const keys = [...await cache.keys()];
  while (keys.length > MAX_ENTRIES) {
    const oldest = keys.shift();
    if (oldest) await cache.delete(oldest);
  }
}

sw.addEventListener("install", () => {
  sw.skipWaiting();
});

sw.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME).map((name) => caches.delete(name)));
    await sw.clients.claim();
  })());
});

sw.addEventListener("fetch", (event) => {
  if (!isPagePreview(event.request)) return;
  let cacheWrite = Promise.resolve();
  const response = (async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(event.request);
    if (cached) return cached;
    const response = await fetch(event.request);
    if (response.ok || response.type === "opaque") {
      cacheWrite = cache.put(event.request, response.clone()).then(() => trimCache(cache));
    }
    return response;
  })();
  event.respondWith(response);
  event.waitUntil(response.then(() => cacheWrite).catch(() => {}));
});
