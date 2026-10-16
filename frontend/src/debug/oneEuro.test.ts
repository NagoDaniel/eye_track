import { describe, expect, it } from "vitest";

import { OneEuroFilter2D, ONE_EURO_RESET_GAP_MS } from "./oneEuro";

/** Deterministic noise in [-1, 1]. */
function noise(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1664525 + 1013904223) % 4294967296;
    return (state / 4294967296) * 2 - 1;
  };
}

const FRAME_MS = 33;

describe("OneEuroFilter2D", () => {
  it("returns the first sample unchanged", () => {
    expect(new OneEuroFilter2D().filter(0, 100, 200)).toEqual([100, 200]);
  });

  it("settles on a constant input", () => {
    const filter = new OneEuroFilter2D();
    let out: [number, number] = [0, 0];
    for (let i = 0; i < 300; i++) out = filter.filter(i * FRAME_MS, 640, 360);
    expect(out[0]).toBeCloseTo(640, 6);
    expect(out[1]).toBeCloseTo(360, 6);
  });

  it("removes a good part of the jitter of a resting gaze", () => {
    const random = noise(3);
    const filter = new OneEuroFilter2D();
    const raw: number[] = [];
    const smooth: number[] = [];
    for (let i = 0; i < 600; i++) {
      const x = 800 + 150 * random(); // +-150 px noise, the pilot's order of magnitude
      const [fx] = filter.filter(i * FRAME_MS, x, 400);
      if (i > 30) {
        raw.push(Math.abs(x - 800));
        smooth.push(Math.abs(fx - 800));
      }
    }
    const mean = (values: number[]): number => values.reduce((a, b) => a + b, 0) / values.length;
    expect(mean(smooth)).toBeLessThan(0.6 * mean(raw));
  });

  it("follows a fast move much sooner than a slow filter would", () => {
    const fast = new OneEuroFilter2D();
    const slow = new OneEuroFilter2D({ beta: 0 }); // fixed 0.5 Hz: no speed-up
    for (let i = 0; i < 60; i++) {
      fast.filter(i * FRAME_MS, 200, 200);
      slow.filter(i * FRAME_MS, 200, 200);
    }
    let a: [number, number] = [0, 0];
    let b: [number, number] = [0, 0];
    for (let i = 60; i < 75; i++) {
      a = fast.filter(i * FRAME_MS, 1400, 800); // a jump across the screen
      b = slow.filter(i * FRAME_MS, 1400, 800);
    }
    expect(a[0]).toBeGreaterThan(b[0]);
    expect(a[0]).toBeGreaterThan(900); // has covered most of the way after half a second
  });

  it("smooths both axes with the same strength", () => {
    const filter = new OneEuroFilter2D();
    for (let i = 0; i < 30; i++) filter.filter(i * FRAME_MS, 0, 0);
    const [x, y] = filter.filter(30 * FRAME_MS, 100, 100);
    expect(x).toBeCloseTo(y, 9);
  });

  it("starts again after a gap (the face was lost) and ignores a timestamp that does not advance", () => {
    const filter = new OneEuroFilter2D();
    for (let i = 0; i < 30; i++) filter.filter(i * FRAME_MS, 0, 0);
    const gap = 30 * FRAME_MS + ONE_EURO_RESET_GAP_MS + 1;
    expect(filter.filter(gap, 900, 500)).toEqual([900, 500]); // no trail from the old position
    expect(filter.filter(gap, 100, 100)).toEqual([100, 100]); // same time: restart, no division by 0
  });

  it("reset forgets the history", () => {
    const filter = new OneEuroFilter2D();
    filter.filter(0, 0, 0);
    filter.filter(FRAME_MS, 10, 10);
    filter.reset();
    expect(filter.filter(2 * FRAME_MS, 500, 500)).toEqual([500, 500]);
  });
});
