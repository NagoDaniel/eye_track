// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StudyConfig, VideoRef } from "../config";
import { type SkipSignal, SkipBus } from "../debug/skip";
import { EventLog, type EventRecord, type Size } from "../eventLog";
import { playbackScreen } from "./playback";

// jsdom has no media pipeline: play/pause are stubbed and the media events are dispatched by hand.
// Each test gets its own document-like event target so that listeners never leak between tests.
const VIDEO: VideoRef = { id: "7", url: "/v/7.mp4", sha256: "x", duration: 10, fps: 25 };

let root: HTMLElement;
let records: EventRecord[];
let log: EventLog;
let intrinsic: (Size | null)[];
let fullscreenElement: Element | null;
let doc: Document & EventTarget;
let play: ReturnType<typeof vi.spyOn>;
let pause: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  document.body.innerHTML = '<main id="app"></main>';
  root = document.getElementById("app") as HTMLElement;
  records = [];
  log = new EventLog(
    (r) => records.push(r),
    () => 0,
  );
  intrinsic = [];
  fullscreenElement = null;
  doc = Object.assign(new EventTarget(), {
    createElement: (tag: string) => document.createElement(tag),
    documentElement: document.documentElement,
  }) as unknown as Document & EventTarget;
  Object.defineProperty(doc, "fullscreenElement", { get: () => fullscreenElement });
  play = vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => Promise.resolve());
  pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
});
afterEach(() => vi.restoreAllMocks());

function start(
  playback: StudyConfig["playback"],
  trackFrames?: (video: HTMLVideoElement) => () => void,
  skip?: SkipSignal,
): { media: HTMLVideoElement; done: Promise<void> } {
  const done = playbackScreen(
    root,
    VIDEO,
    "blob:video",
    playback,
    log,
    { setVideoIntrinsic: (s) => intrinsic.push(s) },
    { doc, trackFrames, skip },
  );
  const media = root.querySelector("video") as HTMLVideoElement;
  let currentTime = 0;
  Object.defineProperty(media, "currentTime", {
    configurable: true,
    get: () => currentTime,
    set: (v: number) => (currentTime = v),
  });
  Object.defineProperty(media, "videoWidth", { configurable: true, value: window.innerWidth * 2 });
  Object.defineProperty(media, "videoHeight", { configurable: true, value: window.innerHeight });
  Object.defineProperty(media, "duration", { configurable: true, value: 10 });
  return { media, done };
}
const types = (): string[] => records.map((r) => r.type);
const overlay = (): HTMLElement => root.querySelector(".player-overlay") as HTMLElement;

describe("playbackScreen debug skip (PLAN §12.1)", () => {
  it("ends the video as if it had ended: logs video_ended, stops frame tracking, resolves", async () => {
    const skip = new SkipBus();
    const calls: string[] = [];
    const { media, done } = start(
      { allow_pause: false, require_fullscreen: false },
      () => {
        calls.push("start");
        return () => calls.push("stop");
      },
      skip,
    );
    media.dispatchEvent(new Event("loadedmetadata"));
    skip.fire();
    await done;
    expect(types()).toContain("video_ended");
    expect(calls).toEqual(["start", "stop"]);
    expect(skip.fire()).toBe(0); // unsubscribed
  });
});

describe("playbackScreen frame tracking (PLAN §5.3)", () => {
  it("starts it once the metadata is loaded and stops it when the video ends", async () => {
    const calls: string[] = [];
    const { media, done } = start({ allow_pause: false, require_fullscreen: false }, (video) => {
      calls.push(video === media ? "start" : "start:other");
      return () => calls.push("stop");
    });
    expect(calls).toEqual([]);
    media.dispatchEvent(new Event("loadedmetadata"));
    media.dispatchEvent(new Event("loadedmetadata")); // a repeated event does not start it twice
    expect(calls).toEqual(["start"]);
    media.dispatchEvent(new Event("ended"));
    await done;
    expect(calls).toEqual(["start", "stop"]);
  });

  it("stops it when the video fails", async () => {
    const calls: string[] = [];
    const { media, done } = start({ allow_pause: false, require_fullscreen: false }, () => {
      calls.push("start");
      return () => calls.push("stop");
    });
    media.dispatchEvent(new Event("loadedmetadata"));
    media.dispatchEvent(new Event("error"));
    await expect(done).rejects.toThrow("could not be played");
    expect(calls).toEqual(["start", "stop"]);
  });
});

describe("playbackScreen", () => {
  it("uses the preloaded URL, no controls, and logs video_load with the content rect when fullscreen is not required", () => {
    const { media } = start({ allow_pause: false, require_fullscreen: false });
    expect(media.getAttribute("src")).toBe("blob:video");
    expect(media.controls).toBe(false);
    media.dispatchEvent(new Event("loadedmetadata"));
    expect(play).toHaveBeenCalledTimes(1);
    expect(records[0]).toMatchObject({
      type: "video_load",
      data: {
        video_id: "7",
        duration: 10,
        width: window.innerWidth * 2,
        height: window.innerHeight,
        rect: {
          x: 0,
          y: window.innerHeight / 4,
          width: window.innerWidth,
          height: window.innerHeight / 2,
        },
      },
    });
    expect(intrinsic).toEqual([{ width: window.innerWidth * 2, height: window.innerHeight }]);
  });

  it("logs play, pause, stall and ended with the media time, then resolves", async () => {
    const { media, done } = start({ allow_pause: false, require_fullscreen: false });
    media.dispatchEvent(new Event("loadedmetadata"));
    media.currentTime = 1.5;
    media.dispatchEvent(new Event("play"));
    media.currentTime = 2;
    media.dispatchEvent(new Event("waiting"));
    media.currentTime = 3;
    media.dispatchEvent(new Event("pause"));
    expect(records.slice(1).map((r) => [r.type, r.data])).toEqual([
      ["video_play", { video_id: "7", media_time: 1.5 }],
      ["video_stall", { video_id: "7", media_time: 2 }],
      ["video_pause", { video_id: "7", media_time: 3 }],
    ]);
    Object.defineProperty(media, "ended", { configurable: true, value: true });
    media.dispatchEvent(new Event("pause")); // browsers fire pause before ended; it is not logged
    media.dispatchEvent(new Event("ended"));
    await done;
    expect(types().slice(-1)).toEqual(["video_ended"]);
    expect(types().filter((t) => t === "video_pause")).toHaveLength(1);
    expect(intrinsic[intrinsic.length - 1]).toBeNull();
  });

  it("does not start while fullscreen is required and missing; starts once it is entered", () => {
    const { media } = start({ allow_pause: false, require_fullscreen: true });
    media.dispatchEvent(new Event("loadedmetadata"));
    expect(play).not.toHaveBeenCalled();
    expect(overlay().hidden).toBe(false);
    expect(overlay().textContent).toContain("Return to fullscreen to continue");
    fullscreenElement = document.documentElement;
    doc.dispatchEvent(new Event("fullscreenchange"));
    expect(play).toHaveBeenCalledTimes(1);
    expect(overlay().hidden).toBe(true);
  });

  it("pauses when fullscreen is left and resumes when it is re-entered", () => {
    fullscreenElement = document.documentElement;
    const { media } = start({ allow_pause: false, require_fullscreen: true });
    media.dispatchEvent(new Event("loadedmetadata"));
    expect(play).toHaveBeenCalledTimes(1);
    fullscreenElement = null;
    doc.dispatchEvent(new Event("fullscreenchange"));
    expect(pause).toHaveBeenCalledTimes(1);
    expect(overlay().hidden).toBe(false);
    fullscreenElement = document.documentElement;
    doc.dispatchEvent(new Event("fullscreenchange"));
    expect(play).toHaveBeenCalledTimes(2);
    expect(overlay().hidden).toBe(true);
  });

  it("does not resume a video it did not pause when fullscreen is re-entered", () => {
    const { media } = start({ allow_pause: true, require_fullscreen: false });
    media.dispatchEvent(new Event("loadedmetadata"));
    play.mockClear();
    fullscreenElement = document.documentElement;
    doc.dispatchEvent(new Event("fullscreenchange"));
    expect(play).not.toHaveBeenCalled();
  });

  it("ignores the space bar unless allow_pause, and toggles pause when it is set", () => {
    const strict = start({ allow_pause: false, require_fullscreen: false });
    strict.media.dispatchEvent(new Event("loadedmetadata"));
    const blocked = new KeyboardEvent("keydown", { key: " ", cancelable: true });
    doc.dispatchEvent(blocked);
    expect(blocked.defaultPrevented).toBe(true);
    expect(pause).not.toHaveBeenCalled();
    strict.media.dispatchEvent(new Event("ended")); // detach this instance's listeners

    const open = start({ allow_pause: true, require_fullscreen: false });
    open.media.dispatchEvent(new Event("loadedmetadata"));
    play.mockClear();
    Object.defineProperty(open.media, "paused", { configurable: true, value: false });
    doc.dispatchEvent(new KeyboardEvent("keydown", { key: " " }));
    expect(pause).toHaveBeenCalledTimes(1);
    Object.defineProperty(open.media, "paused", { configurable: true, value: true });
    doc.dispatchEvent(new KeyboardEvent("keydown", { key: " " }));
    expect(play).toHaveBeenCalledTimes(1);
  });

  it("undoes a seek", () => {
    const { media } = start({ allow_pause: false, require_fullscreen: false });
    media.dispatchEvent(new Event("loadedmetadata"));
    media.currentTime = 4;
    media.dispatchEvent(new Event("timeupdate"));
    media.currentTime = 9; // the viewer jumps ahead
    media.dispatchEvent(new Event("seeking"));
    expect(media.currentTime).toBe(4);
    media.currentTime = 4.2; // normal progress is not a seek
    media.dispatchEvent(new Event("seeking"));
    expect(media.currentTime).toBe(4.2);
  });

  it("rejects when the video cannot be played", async () => {
    const { media, done } = start({ allow_pause: false, require_fullscreen: false });
    media.dispatchEvent(new Event("error"));
    await expect(done).rejects.toThrow(/could not be played/);
  });
});
