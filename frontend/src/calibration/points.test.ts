import { describe, expect, it } from "vitest";

import type { Rect } from "../eventLog";
import { assertPointCounts, calibrationPoints, contentRect, validationPoints } from "./points";

/** Deterministic pseudo-random source in [0, 1). */
function seeded(seed: number): () => number {
  let s = seed;
  return () => {
    s = (s * 1664525 + 1013904223) % 4294967296;
    return s / 4294967296;
  };
}

const key = (p: { x: number; y: number }): string => `${p.x.toFixed(6)},${p.y.toFixed(6)}`;

describe("calibrationPoints", () => {
  const W = 1000;
  const H = 800;
  const MARGIN = 0.06;

  it("has 13 distinct points with the centre first", () => {
    const points = calibrationPoints(W, H, MARGIN, seeded(1));
    expect(points).toHaveLength(13);
    expect(new Set(points.map(key)).size).toBe(13);
    expect(points[0]).toEqual({ x: 500, y: 400 });
  });

  it("is a 3x3 grid inset by the margin plus the 4 quadrant centres (exact values)", () => {
    const points = calibrationPoints(W, H, MARGIN, seeded(2));
    const expected = new Set<string>();
    for (const y of [48, 400, 752]) for (const x of [60, 500, 940]) expected.add(key({ x, y }));
    for (const y of [200, 600]) for (const x of [250, 750]) expected.add(key({ x, y }));
    expect(new Set(points.map(key))).toEqual(expected);
  });

  it("randomises the order of the other 12 points but never moves the centre", () => {
    const orders = new Set<string>();
    for (let seed = 1; seed <= 20; seed++) {
      const points = calibrationPoints(W, H, MARGIN, seeded(seed));
      expect(points[0]).toEqual({ x: 500, y: 400 });
      orders.add(points.map(key).join("|"));
    }
    expect(orders.size).toBeGreaterThan(1);
  });

  it("uses the injected random source (same source, same order)", () => {
    const a = calibrationPoints(W, H, MARGIN, seeded(7)).map(key);
    const b = calibrationPoints(W, H, MARGIN, seeded(7)).map(key);
    expect(a).toEqual(b);
  });

  it("rejects a margin that would make points coincide or leave the screen", () => {
    expect(() => calibrationPoints(W, H, 0)).toThrow(/margin/);
    expect(() => calibrationPoints(W, H, 0.25)).toThrow(/margin/);
  });
});

describe("validationPoints", () => {
  it("is the centre plus the 4 inner corners at 25 % / 75 %", () => {
    const points = validationPoints(1000, 800);
    expect(points).toEqual([
      { x: 500, y: 400 },
      { x: 250, y: 200 },
      { x: 750, y: 200 },
      { x: 250, y: 600 },
      { x: 750, y: 600 },
    ]);
  });
});

describe("assertPointCounts", () => {
  const ok = { calibration: { points: 13, dot_duration_ms: 1500, margin: 0.06 }, validation: { points: 5, before_video: true, between_videos: true, after_last_video: true } };
  it("passes for 13 and 5", () => expect(() => assertPointCounts(ok)).not.toThrow());
  it("fails loudly otherwise", () => {
    expect(() => assertPointCounts({ ...ok, calibration: { ...ok.calibration, points: 9 } })).toThrow(/calibration\.points is 9/);
    expect(() => assertPointCounts({ ...ok, validation: { ...ok.validation, points: 4 } })).toThrow(/validation\.points is 4/);
  });
});

function expectRect(actual: Rect, expected: Rect): void {
  expect(actual.x).toBeCloseTo(expected.x, 6);
  expect(actual.y).toBeCloseTo(expected.y, 6);
  expect(actual.width).toBeCloseTo(expected.width, 6);
  expect(actual.height).toBeCloseTo(expected.height, 6);
}

describe("contentRect", () => {
  it("letterboxes a video that is wider than the viewport (bars above and below)", () => {
    // 2:1 video in a 1000x800 viewport -> 1000x500, centred vertically
    expectRect(contentRect({ width: 1000, height: 800 }, 1920, 960), { x: 0, y: 150, width: 1000, height: 500 });
  });

  it("pillarboxes a video that is taller than the viewport (bars left and right)", () => {
    // 1:2 video in a 1000x800 viewport -> 400x800, centred horizontally
    expectRect(contentRect({ width: 1000, height: 800 }, 540, 1080), { x: 300, y: 0, width: 400, height: 800 });
  });

  it("fills the viewport when the aspect ratios match, and scales up small videos", () => {
    expectRect(contentRect({ width: 1600, height: 900 }, 320, 180), { x: 0, y: 0, width: 1600, height: 900 });
  });

  it("is computed from the intrinsic size, so it does not depend on any element box", () => {
    expectRect(contentRect({ width: 1280, height: 720 }, 1000, 1000), { x: 280, y: 0, width: 720, height: 720 });
  });

  it("throws without an intrinsic size", () => {
    expect(() => contentRect({ width: 100, height: 100 }, 0, 0)).toThrow(/intrinsic size/);
  });
});
