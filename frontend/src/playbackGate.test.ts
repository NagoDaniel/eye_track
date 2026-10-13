import { describe, expect, it } from "vitest";

import { fullscreenGate, isSeekAttempt } from "./playbackGate";

describe("fullscreenGate", () => {
  it("pauses when fullscreen is left, and only once", () => {
    const left = fullscreenGate({ requireFullscreen: true, isFullscreen: false, pausedForFullscreen: false });
    expect(left).toEqual({ action: "pause", pausedForFullscreen: true });
    expect(fullscreenGate({ requireFullscreen: true, isFullscreen: false, pausedForFullscreen: true })).toEqual({
      action: "none",
      pausedForFullscreen: true,
    });
  });

  it("resumes when fullscreen is re-entered, but only if the gate paused the video", () => {
    expect(fullscreenGate({ requireFullscreen: true, isFullscreen: true, pausedForFullscreen: true })).toEqual({
      action: "resume",
      pausedForFullscreen: false,
    });
    expect(fullscreenGate({ requireFullscreen: true, isFullscreen: true, pausedForFullscreen: false })).toEqual({
      action: "none",
      pausedForFullscreen: false,
    });
  });

  it("does nothing when the study does not require fullscreen", () => {
    for (const isFullscreen of [true, false]) {
      expect(fullscreenGate({ requireFullscreen: false, isFullscreen, pausedForFullscreen: false })).toEqual({
        action: "none",
        pausedForFullscreen: false,
      });
    }
  });

  it("follows leave, re-enter, leave again", () => {
    let held = false;
    const actions: string[] = [];
    for (const isFullscreen of [true, false, false, true, false]) {
      const r = fullscreenGate({ requireFullscreen: true, isFullscreen, pausedForFullscreen: held });
      held = r.pausedForFullscreen;
      actions.push(r.action);
    }
    expect(actions).toEqual(["none", "pause", "none", "resume", "pause"]);
  });
});

describe("isSeekAttempt", () => {
  it("ignores normal playback drift and flags a jump", () => {
    expect(isSeekAttempt(10.2, 10, 0.5)).toBe(false);
    expect(isSeekAttempt(10.5, 10, 0.5)).toBe(false);
    expect(isSeekAttempt(10.6, 10, 0.5)).toBe(true);
    expect(isSeekAttempt(2, 10, 0.5)).toBe(true);
  });
});
