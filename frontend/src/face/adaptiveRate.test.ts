import { describe, expect, it } from "vitest";

import {
  AdaptiveRate,
  FrameGate,
  RATE_MIN_FPS,
  RATE_RECOVER_MS,
  RATE_STEP_DOWN,
  RATE_STEP_UP,
  RATE_WINDOW_MS,
  targetFps,
} from "./adaptiveRate";

/** Reports one batch of 30 frames per `step` ms from `from` to `to`, `dropped` of them dropped. */
function run(rate: AdaptiveRate, from: number, to: number, dropped: number, step = 500) {
  for (let t = from; t < to; t += step) rate.report(t, 30 - dropped, dropped);
}

describe("targetFps", () => {
  it("is the video fps capped at the camera fps", () => {
    expect(targetFps(24, 30)).toBe(24);
    expect(targetFps(60, 30)).toBe(30);
    expect(targetFps(25, 15)).toBe(15);
  });

  it("is the camera fps without a video", () => {
    expect(targetFps(null, 30)).toBe(30);
  });

  it("rejects a bad camera fps", () => {
    expect(() => targetFps(24, 0)).toThrow();
  });
});

describe("AdaptiveRate", () => {
  it("starts at the target and holds without drops", () => {
    const rate = new AdaptiveRate({ targetFps: 30, minFps: 8 });
    expect(rate.currentFps()).toBe(30);
    run(rate, 0, 20000, 0);
    expect(rate.currentFps()).toBe(30);
  });

  it("uses RATE_MIN_FPS by default", () => {
    const rate = new AdaptiveRate({ targetFps: 30 });
    run(rate, 0, 60000, 10);
    expect(rate.currentFps()).toBe(RATE_MIN_FPS);
  });

  it("steps down when more than 5 % are dropped, at most once per window", () => {
    const rate = new AdaptiveRate({ targetFps: 30, minFps: 8 });
    rate.report(0, 28, 2); // 6.7 %
    expect(rate.currentFps()).toBeCloseTo(30 * RATE_STEP_DOWN, 10);
    rate.report(500, 28, 2);
    rate.report(1000, 28, 2);
    expect(rate.currentFps()).toBeCloseTo(30 * RATE_STEP_DOWN, 10);
    rate.report(RATE_WINDOW_MS, 28, 2);
    expect(rate.currentFps()).toBeCloseTo(30 * RATE_STEP_DOWN ** 2, 10);
  });

  it("holds at exactly 5 % and below", () => {
    const rate = new AdaptiveRate({ targetFps: 30, minFps: 8 });
    for (let t = 0; t < 10000; t += 500) rate.report(t, 95, 5);
    expect(rate.currentFps()).toBe(30);
  });

  it("does not go below the minimum", () => {
    const rate = new AdaptiveRate({ targetFps: 30, minFps: 8 });
    run(rate, 0, 60000, 10);
    expect(rate.currentFps()).toBe(8);
  });

  it("raises by one step after 5 s without drops", () => {
    const rate = new AdaptiveRate({ targetFps: 30, minFps: 8 });
    rate.report(0, 20, 10);
    const lowered = rate.currentFps();
    expect(lowered).toBeCloseTo(22.5, 10);
    run(rate, 500, RATE_RECOVER_MS, 0);
    expect(rate.currentFps()).toBe(lowered);
    rate.report(RATE_RECOVER_MS, 30, 0);
    expect(rate.currentFps()).toBeCloseTo(lowered * RATE_STEP_UP, 10);
    // the next step needs another quiet period
    rate.report(RATE_RECOVER_MS + 500, 30, 0);
    expect(rate.currentFps()).toBeCloseTo(lowered * RATE_STEP_UP, 10);
  });

  it("recovers to the target and no further", () => {
    const rate = new AdaptiveRate({ targetFps: 30, minFps: 8 });
    run(rate, 0, 8000, 10);
    expect(rate.currentFps()).toBeLessThan(15);
    run(rate, 8000, 80000, 0);
    expect(rate.currentFps()).toBe(30);
  });

  it("a dropped frame restarts the recovery clock", () => {
    const rate = new AdaptiveRate({ targetFps: 30, minFps: 8 });
    rate.report(0, 20, 10);
    const lowered = rate.currentFps();
    run(rate, 500, 4000, 0);
    rate.report(4000, 99, 1); // 1 %: no step down, but not quiet either
    run(rate, 4500, 8500, 0);
    expect(rate.currentFps()).toBe(lowered);
    rate.report(9000, 30, 0);
    expect(rate.currentFps()).toBeGreaterThan(lowered);
  });

  it("follows a new target: lowered at once, raised by recovery", () => {
    const rate = new AdaptiveRate({ targetFps: 30, minFps: 8 });
    rate.setTarget(24);
    expect(rate.currentFps()).toBe(24);
    rate.report(0, 20, 10);
    rate.setTarget(30);
    expect(rate.currentFps()).toBeLessThan(24);
    run(rate, 500, 30000, 0);
    expect(rate.currentFps()).toBe(30);
  });

  it("keeps the floor at or below a target that is lower than the minimum", () => {
    const rate = new AdaptiveRate({ targetFps: 5, minFps: 8 });
    run(rate, 0, 10000, 10);
    expect(rate.currentFps()).toBe(5);
  });

  it("rejects bad input", () => {
    expect(() => new AdaptiveRate({ targetFps: 0 })).toThrow();
    expect(() => new AdaptiveRate({ targetFps: 30, minFps: 0 })).toThrow();
    expect(() => new AdaptiveRate({ targetFps: 30 }).report(0, -1, 0)).toThrow();
  });
});

describe("FrameGate", () => {
  function passed(fps: number, cameraFps: number, seconds: number, jitterMs = 0): number[] {
    const gate = new FrameGate({ currentFps: () => fps });
    const times: number[] = [];
    const step = 1000 / cameraFps;
    for (let i = 0; i * step < seconds * 1000; i++) {
      const t = 5000 + i * step + (i % 2 === 0 ? jitterMs : -jitterMs);
      if (gate.shouldProcess(t)) times.push(t);
    }
    return times;
  }

  it("passes every second frame of a 30 fps camera at 15 fps", () => {
    const times = passed(15, 30, 4);
    expect(times).toHaveLength(60);
    for (let i = 1; i < times.length; i++) {
      expect((times[i] as number) - (times[i - 1] as number)).toBeCloseTo(66.67, 1);
    }
  });

  it("passes all frames when the rate equals the camera rate, even with timing jitter", () => {
    expect(passed(30, 30, 4)).toHaveLength(120);
    expect(passed(30, 30, 4, 1.5).length).toBeGreaterThanOrEqual(118);
  });

  it("averages the target rate when it does not divide the camera rate", () => {
    const times = passed(24, 30, 10);
    expect(times.length).toBeGreaterThanOrEqual(238);
    expect(times.length).toBeLessThanOrEqual(242);
  });

  it("keeps the spacing at a low rate", () => {
    const times = passed(10, 30, 5);
    expect(times.length).toBeGreaterThanOrEqual(49);
    expect(times.length).toBeLessThanOrEqual(51);
    for (let i = 1; i < times.length; i++) {
      expect((times[i] as number) - (times[i - 1] as number)).toBeGreaterThanOrEqual(90);
    }
  });

  it("does not make up for a pause with a burst", () => {
    const gate = new FrameGate({ currentFps: () => 10 });
    expect(gate.shouldProcess(0)).toBe(true);
    // 2 s without frames, then frames at 30 fps
    const after = [2000, 2033, 2067, 2100, 2133, 2167, 2200].filter((t) => gate.shouldProcess(t));
    expect(after).toEqual([2000, 2100, 2200]);
  });

  it("follows a changed rate", () => {
    let fps = 30;
    const gate = new FrameGate({ currentFps: () => fps });
    const first = [0, 33, 67].filter((t) => gate.shouldProcess(t));
    expect(first).toEqual([0, 33, 67]);
    fps = 10;
    const later = [100, 133, 167, 200, 233, 267].filter((t) => gate.shouldProcess(t));
    expect(later.length).toBeLessThanOrEqual(3);
    expect(later.length).toBeGreaterThanOrEqual(2);
  });
});
