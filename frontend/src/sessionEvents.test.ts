// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { EventLog, type EventRecord } from "./eventLog";
import { SessionEvents } from "./sessionEvents";

let fullscreenElement: Element | null = null;
let records: EventRecord[];
let events: SessionEvents;

beforeEach(() => {
  fullscreenElement = null;
  Object.defineProperty(document, "fullscreenElement", { configurable: true, get: () => fullscreenElement });
  records = [];
  events = new SessionEvents(new EventLog((r) => records.push(r)));
});
afterEach(() => events.stop());

const types = (): string[] => records.map((r) => r.type);

describe("SessionEvents", () => {
  it("logs the viewport at start, with the screen, pixel ratio and no video rect", () => {
    events.start();
    expect(types()).toEqual(["viewport_change"]);
    expect(records[0]?.data).toEqual({
      viewport: { width: window.innerWidth, height: window.innerHeight },
      screen: { width: window.screen.width, height: window.screen.height },
      device_pixel_ratio: window.devicePixelRatio,
      video_rect: null,
    });
  });

  it("logs a viewport_change on every resize, with the video content rect while a video is shown", () => {
    events.start();
    events.setVideoIntrinsic({ width: window.innerWidth * 2, height: window.innerHeight }); // twice as wide
    const rect = (records[1]?.data["video_rect"] ?? null) as { x: number; y: number; width: number; height: number } | null;
    expect(rect).toEqual({ x: 0, y: window.innerHeight / 4, width: window.innerWidth, height: window.innerHeight / 2 });
    window.dispatchEvent(new Event("resize"));
    expect(types()).toEqual(["viewport_change", "viewport_change", "viewport_change"]);
    events.setVideoIntrinsic(null);
    expect(records[3]?.data["video_rect"]).toBeNull();
  });

  it("logs fullscreen, visibility, blur and focus", () => {
    events.start();
    records.length = 0;
    fullscreenElement = document.documentElement;
    document.dispatchEvent(new Event("fullscreenchange"));
    fullscreenElement = null;
    document.dispatchEvent(new Event("fullscreenchange"));
    document.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("blur"));
    window.dispatchEvent(new Event("focus"));
    expect(records.map((r) => [r.type, r.data])).toEqual([
      ["fullscreen_change", { fullscreen: true }],
      ["fullscreen_change", { fullscreen: false }],
      ["visibility_change", { state: document.visibilityState }],
      ["window_blur", {}],
      ["window_focus", {}],
    ]);
  });

  it("stops listening after stop()", () => {
    events.start();
    events.stop();
    records.length = 0;
    window.dispatchEvent(new Event("blur"));
    window.dispatchEvent(new Event("resize"));
    expect(records).toEqual([]);
  });
});
