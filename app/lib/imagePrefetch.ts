type ImageBufferEntry = {
  promise: Promise<ArrayBuffer>;
};

const imageBuffers = new Map<string, ImageBufferEntry>();
const jsonResponses = new Map<string, Promise<unknown>>();

export function prefetchImageBuffer(url: string) {
  const existing = imageBuffers.get(url);
  if (existing) return existing.promise;
  const entry: ImageBufferEntry = {
    promise: fetch(url, { credentials: "same-origin" }).then(async (response) => {
      if (!response.ok) throw new Error(`影像预取失败：${response.status}`);
      return response.arrayBuffer();
    }),
  };
  imageBuffers.set(url, entry);
  entry.promise.then(() => {
    if (typeof window !== "undefined") {
      window.setTimeout(() => {
        if (imageBuffers.get(url) === entry) imageBuffers.delete(url);
      }, 60_000);
    }
  }).catch(() => {
    if (imageBuffers.get(url) === entry) imageBuffers.delete(url);
  });
  return entry.promise;
}

export function releaseImageBuffer(url: string) {
  imageBuffers.delete(url);
}

export function prefetchJson<T>(url: string): Promise<T> {
  const existing = jsonResponses.get(url);
  if (existing) return existing as Promise<T>;
  const request = fetch(url, { credentials: "same-origin" }).then(async (response) => {
    if (!response.ok) throw new Error(`数据预取失败：${response.status}`);
    return response.json() as Promise<T>;
  });
  jsonResponses.set(url, request);
  request.then(() => {
    if (typeof window !== "undefined") {
      window.setTimeout(() => {
        if (jsonResponses.get(url) === request) jsonResponses.delete(url);
      }, 60_000);
    }
  }).catch(() => {
    if (jsonResponses.get(url) === request) jsonResponses.delete(url);
  });
  return request;
}

export function releaseJson(url: string) {
  jsonResponses.delete(url);
}
