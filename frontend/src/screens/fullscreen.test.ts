// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fullscreenScreen } from "./fullscreen";

let root: HTMLElement;
beforeEach(() => {
  document.body.innerHTML = '<main id="app"></main>';
  root = document.getElementById("app") as HTMLElement;
});

function fakeDoc(requestFullscreen: () => Promise<void>): Document {
  return {
    fullscreenElement: null,
    documentElement: { requestFullscreen },
  } as unknown as Document;
}

describe("fullscreenScreen", () => {
  it("is skipped when fullscreen is not required", async () => {
    const request = vi.fn();
    await fullscreenScreen(root, false, fakeDoc(request));
    expect(request).not.toHaveBeenCalled();
    expect(root.textContent).toBe("");
  });

  it("resolves after the participant enters fullscreen", async () => {
    const request = vi.fn().mockResolvedValue(undefined);
    const done = fullscreenScreen(root, true, fakeDoc(request));
    (root.querySelector("button") as HTMLButtonElement).click();
    await done;
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("shows the refusal and lets the participant try again", async () => {
    const request = vi.fn().mockRejectedValueOnce(new Error("denied")).mockResolvedValue(undefined);
    const done = fullscreenScreen(root, true, fakeDoc(request));
    (root.querySelector("button") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(root.textContent).toContain("denied"));
    (root.querySelector("button") as HTMLButtonElement).click();
    await done;
    expect(request).toHaveBeenCalledTimes(2);
  });
});
