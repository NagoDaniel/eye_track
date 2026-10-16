// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

import { sha256Hex, type FetchFn } from "../api";
import type { VideoRef } from "../config";
import { preloadScreen } from "./preload";

let root: HTMLElement;
beforeEach(() => {
  document.body.innerHTML = '<main id="app"></main>';
  root = document.getElementById("app") as HTMLElement;
});

describe("preloadScreen", () => {
  it("blocks with a clear message when a video's hash does not match, and retries on request", async () => {
    const bytes = new TextEncoder().encode("the real bytes");
    const ref: VideoRef = { id: "7", url: "/v/7.mp4", sha256: await sha256Hex(bytes), duration: 1, fps: 25 };
    let served: Uint8Array = new TextEncoder().encode("corrupted bytes");
    const fetchFn: FetchFn = () => Promise.resolve(new Response(served as BodyInit));

    let result: Map<string, string> | null = null;
    void preloadScreen(root, [ref], { fetchFn, createObjectUrl: () => "blob:ok" }).then((r) => (result = r));

    await vi.waitFor(() => expect(root.textContent).toContain("checksum mismatch"));
    expect(root.textContent).toContain("Video 7 was damaged during download");
    expect(result).toBeNull();

    served = bytes; // the retry gets good bytes
    (root.querySelector("button") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(result).not.toBeNull());
    expect((result as unknown as Map<string, string>).get("7")).toBe("blob:ok");
  });
});
