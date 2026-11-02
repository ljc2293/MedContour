import assert from "node:assert/strict";
import test from "node:test";

import { prefetchImageBuffer, prefetchJson, releaseImageBuffer, releaseJson } from "../app/lib/imagePrefetch.ts";

test("concurrent image prefetches share one request and can be released", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return new Response(new Uint8Array([1, 2, 3]));
  };
  try {
    const url = "/api/test/shared-preview.nii.gz";
    const [first, second] = await Promise.all([prefetchImageBuffer(url), prefetchImageBuffer(url)]);
    assert.equal(calls, 1);
    assert.equal(first, second);
    assert.deepEqual([...new Uint8Array(first)], [1, 2, 3]);
    releaseImageBuffer(url);
    await prefetchImageBuffer(url);
    assert.equal(calls, 2);
    releaseImageBuffer(url);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("concurrent JSON prefetches share one request and can be released", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return new Response(JSON.stringify({ available: true }), { headers: { "Content-Type": "application/json" } });
  };
  try {
    const url = "/api/test/pancreas-reference/contours";
    const [first, second] = await Promise.all([prefetchJson(url), prefetchJson(url)]);
    assert.equal(calls, 1);
    assert.deepEqual(first, { available: true });
    assert.equal(first, second);
    releaseJson(url);
    await prefetchJson(url);
    assert.equal(calls, 2);
    releaseJson(url);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
