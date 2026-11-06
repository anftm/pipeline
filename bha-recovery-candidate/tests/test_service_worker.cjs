const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const handlers = {};
let networkCalls = 0;
const stored = new Map();
const cache = {
  keys: async () => [...stored.keys()].map((url) => ({ url })),
  delete: async (request) => stored.delete(request.url),
  match: async (request) => stored.get(request.url),
  put: async (request, response) => stored.set(request.url, response),
};
const deletedCaches = [];
let claimed = false;
const caches = { open: async () => cache, keys: async () => ["bha-page-webp-v0.9.0", "bha-page-webp-v1.0.0", "reader-downloads", "another-app-cache"],
  delete: async (name) => { deletedCaches.push(name); return true; } };
const self = { addEventListener: (type, fn) => { handlers[type] = fn; },
  skipWaiting: async () => {}, clients: { claim: async () => { claimed = true; } } };
const fetch = async () => {
  networkCalls++;
  return { ok: true, type: "basic", clone() { return this; } };
};
const workerPath = path.join(__dirname, "../static/sw.js");
vm.runInNewContext(fs.readFileSync(workerPath, "utf8"), { self, caches, fetch, URL }, { filename: workerPath });

async function dispatch(url, method = "GET") {
  let response;
  let lifetime;
  handlers.fetch({ request: { url, method }, respondWith(value) { response = value; }, waitUntil(value) { lifetime = value; } });
  if (!response) return undefined;
  const result = await response;
  await lifetime;
  return result;
}

(async () => {
  let activation;
  handlers.activate({waitUntil(value) { activation = value; }});
  await activation;
  assert.deepEqual(deletedCaches, ["bha-page-webp-v0.9.0"]);
  assert.equal(claimed, true);
  assert.equal(await dispatch("https://example.test/api/search"), undefined);
  assert.equal(await dispatch("https://example.test/api/preview/source-page/x", "POST"), undefined);
  // The worker is intentionally image-only: no cached JS, HTML, document
  // preview JSON or submissions may be treated as fresh by this code.
  for (const [url, method] of [
    ["https://example.test/", "GET"],
    ["https://example.test/static/app.js?v=old", "GET"],
    ["https://example.test/api/preview/doc-1", "GET"],
    ["https://example.test/api/proofread", "POST"],
    ["https://example.test/api/parse", "POST"],
  ]) assert.equal(await dispatch(url, method), undefined);
  assert.equal(networkCalls, 0);
  const target = "https://example.test/api/preview/source-page/";
  for (let index = 0; index < 55; index++) await dispatch(target + index);
  assert.equal(stored.size, 50);
  assert.equal(stored.has(target + "0"), false);
  await dispatch(target + "54");
  assert.equal(networkCalls, 55);
  console.log("BHA service worker page cache and bounded eviction passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
