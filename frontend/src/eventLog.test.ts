import { describe, expect, it } from "vitest";

import { clockOrigin, now } from "./clock";
import { EVENT_TYPES, EventLog, type EventRecord } from "./eventLog";

describe("EventLog", () => {
  it("makes records of shape {t, type, data} and hands them to the sink in order", () => {
    const seen: EventRecord[] = [];
    const log = new EventLog((r) => seen.push(r));
    log.log("fullscreen_change", { fullscreen: true });
    log.log("visibility_change", { state: "hidden" });
    log.log("window_blur", {});
    expect(seen.map((r) => r.type)).toEqual(["fullscreen_change", "visibility_change", "window_blur"]);
    for (const r of seen) {
      expect(Object.keys(r).sort()).toEqual(["data", "t", "type"]);
      expect(typeof r.t).toBe("number");
      expect(EVENT_TYPES).toContain(r.type);
    }
    expect(seen[0]?.data).toEqual({ fullscreen: true });
  });

  it("t is monotonic and comes from performance.now()", () => {
    const seen: EventRecord[] = [];
    const log = new EventLog((r) => seen.push(r));
    const before = performance.now();
    for (let i = 0; i < 200; i++) log.log("window_focus", {});
    const after = performance.now();
    const ts = seen.map((r) => r.t);
    expect(ts).toEqual([...ts].sort((a, b) => a - b));
    expect(ts[0]).toBeGreaterThanOrEqual(before);
    expect(ts[ts.length - 1]).toBeLessThanOrEqual(after);
  });

  it("uses the injected clock", () => {
    let t = 10;
    const seen: EventRecord[] = [];
    const log = new EventLog((r) => seen.push(r), () => (t += 5));
    log.log("window_blur", {});
    log.log("window_focus", {});
    expect(seen.map((r) => r.t)).toEqual([15, 20]);
  });

  it("rejects unknown event types and wrong payloads at compile time", () => {
    const log = new EventLog(() => undefined);
    // @ts-expect-error not an event type
    log.log("no_such_event", {});
    // @ts-expect-error video_play needs media_time
    log.log("video_play", { video_id: "1" });
    expect(true).toBe(true);
  });
});

describe("clock", () => {
  it("now() is performance.now()", () => {
    const a = now();
    const b = now();
    expect(b).toBeGreaterThanOrEqual(a);
    expect(Math.abs(a - performance.now())).toBeLessThan(1000);
  });

  it("captures the clock origin once", () => {
    const first = clockOrigin();
    expect(first.time_origin).toBe(performance.timeOrigin);
    expect(clockOrigin()).toBe(first);
  });
});
