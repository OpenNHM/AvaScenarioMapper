/* Fetches + parses shard GeoJSON off the main thread. Protocol: post
   {id, url} in, get back {id, data} or {id, error} — one message per
   request, no shared state between requests. */
"use strict";

self.onmessage = async (ev) => {
  const { id, url } = ev.data;
  try {
    const data = await fetchJSON(url);
    self.postMessage({ id, data });
  } catch (err) {
    self.postMessage({ id, error: err && err.message ? err.message : String(err) });
  }
};

async function fetchJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(url + " → HTTP " + r.status);
  if (url.endsWith(".gz")) {
    if (!self.DecompressionStream) {
      throw new Error("This browser is missing DecompressionStream — needed to read " + url);
    }
    const stream = r.body.pipeThrough(new DecompressionStream("gzip"));
    return new Response(stream).json();
  }
  return r.json();
}
