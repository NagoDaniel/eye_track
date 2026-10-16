import { describe, expect, it } from "vitest";

import { Ema, RATE_WINDOW_MS, RateCounter } from "./counters";

function fakeClock(start = 0): { now: () => number; advance: (ms: number) => void } {
  let t = start;
  return { now: () => t, advance: (ms) => (t += ms) };
}

describe("RateCounter", () => {
  it("is 0 before any time has passed or event happened", () => {
    const c = fakeClock(1000);
    const r = new RateCounter(c.now);
    expect(r.rate()).toBe(0);
    c.advance(500);
    expect(r.rate()).toBe(0);
  });

  it("measures a steady 30 events per second over the window", () => {
    const c = fakeClock(5000);
    const r = new RateCounter(c.now);
    for (let i = 0; i < 90; i++) {
      c.advance(1000 / 30);
      r.tick();
    }
    // Floating-point timestamps put 60 or 61 events in the window.
    expect(r.rate()).toBeGreaterThanOrEqual(29.9);
    expect(r.rate()).toBeLessThanOrEqual(30.6);
  });

  it("uses the elapsed time before one window has passed", () => {
    const c = fakeClock();
    const r = new RateCounter(c.now);
    c.advance(500);
    r.tick();
    r.tick();
    expect(r.rate()).toBeCloseTo(4, 5);
  });

  it("forgets old events and falls back to 0 when they stop", () => {
    const c = fakeClock();
    const r = new RateCounter(c.now);
    for (let i = 0; i < 10; i++) {
      c.advance(100);
      r.tick();
    }
    expect(r.rate()).toBeGreaterThan(5);
    c.advance(RATE_WINDOW_MS + 1);
    expect(r.rate()).toBe(0);
  });

  it("follows a change of rate within one window", () => {
    const c = fakeClock();
    const r = new RateCounter(c.now);
    for (let i = 0; i < 100; i++) {
      c.advance(50); // 20 per second
      r.tick();
    }
    for (let i = 0; i < 40; i++) {
      c.advance(100); // 10 per second, for exactly one window
      r.tick();
    }
    expect(r.rate()).toBeCloseTo(10, 0);
  });

  it("takes the window from its argument", () => {
    const c = fakeClock();
    const r = new RateCounter(c.now, 1000);
    for (let i = 0; i < 4; i++) {
      c.advance(250);
      r.tick();
    }
    c.advance(1);
    expect(r.rate()).toBeCloseTo(4, 0);
  });
});

describe("Ema", () => {
  it("is null before the first sample, then starts at it", () => {
    const e = new Ema(0.5);
    expect(e.value).toBeNull();
    e.add(10);
    expect(e.value).toBe(10);
  });

  it("moves by alpha towards each new sample", () => {
    const e = new Ema(0.5);
    e.add(10);
    e.add(20);
    expect(e.value).toBe(15);
    e.add(15);
    expect(e.value).toBe(15);
  });

  it("converges to a constant input with the default smoothing", () => {
    const e = new Ema();
    e.add(0);
    for (let i = 0; i < 200; i++) e.add(8);
    expect(e.value).toBeCloseTo(8, 3);
  });
});
