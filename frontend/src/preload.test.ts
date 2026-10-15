import { describe, expect, it } from "vitest";

import { sha256Hex, type FetchFn } from "./api";
import type { VideoRef } from "./config";
import { IntegrityError, overallProgress, preloadVideos } from "./preload";

const enc = (s: string): Uint8Array => new TextEncoder().encode(s);

async function video(id: string, content: string): Promise<{ ref: VideoRef; body: Uint8Array }> {
  const body = enc(content);
  return { ref: { id, url: `/media/videos/${id}.mp4`, sha256: await sha256Hex(body), duration: 1 }, body };
}

function serve(files: Record<string, Uint8Array>, status = 200): FetchFn {
  return (url) => {
    const body = files[url];
    if (body === undefined || status !== 200) return Promise.resolve(new Response("nope", { status: status === 200 ? 404 : status }));
    return Promise.resolve(new Response(body as BodyInit, { headers: { "Content-Length": String(body.length) } }));
  };
}

describe("preloadVideos", () => {
  it("downloads every video, verifies the hash, reports progress and returns object URLs", async () => {
    const a = await video("1", "first video bytes");
    const b = await video("2", "second");
    const progress: number[] = [];
    const urls = await preloadVideos([a.ref, b.ref], (f) => progress.push(f), {
      fetchFn: serve({ [a.ref.url]: a.body, [b.ref.url]: b.body }),
      createObjectUrl: () => "blob:fake",
    });
    expect([...urls.keys()]).toEqual(["1", "2"]);
    expect(urls.get("1")).toBe("blob:fake");
    expect(progress[0]).toBe(0);
    expect(progress[progress.length - 1]).toBe(1);
    expect(progress).toEqual([...progress].sort((x, y) => x - y));
  });

  it("throws IntegrityError on a hash mismatch and creates no object URL", async () => {
    const good = await video("1", "expected bytes");
    const created: string[] = [];
    const bad = { ...good.ref, sha256: "0".repeat(64) };
    await expect(
      preloadVideos([bad], () => undefined, {
        fetchFn: serve({ [good.ref.url]: good.body }),
        createObjectUrl: () => (created.push("x"), "blob:x"),
      }),
    ).rejects.toBeInstanceOf(IntegrityError);
    expect(created).toEqual([]);
  });

  it("accepts an upper-case sha256 from the server", async () => {
    const v = await video("1", "abc");
    await preloadVideos([{ ...v.ref, sha256: v.ref.sha256.toUpperCase() }], () => undefined, {
      fetchFn: serve({ [v.ref.url]: v.body }),
      createObjectUrl: () => "blob:x",
    });
  });

  it("fails loudly on an HTTP error", async () => {
    const v = await video("1", "abc");
    await expect(preloadVideos([v.ref], () => undefined, { fetchFn: serve({}) })).rejects.toThrow(/HTTP 404/);
  });
});

describe("overallProgress", () => {
  it("is the mean", () => {
    expect(overallProgress([1, 0.5, 0])).toBe(0.5);
    expect(overallProgress([])).toBe(1);
  });
});
