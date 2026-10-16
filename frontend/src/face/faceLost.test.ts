import { describe, expect, it } from "vitest";

import {
  FACE_LOST_BANNER_MS,
  FACE_LOST_DEBOUNCE_MS,
  type FaceLostEvent,
  FaceLostTracker,
} from "./faceLost";

const FRAME_MS = 33;

/** Feeds frames every FRAME_MS from `from` (inclusive) to `to` (exclusive); returns all events. */
function feed(tracker: FaceLostTracker, from: number, to: number, hasFace: boolean) {
  const events: FaceLostEvent[] = [];
  for (let t = from; t < to; t += FRAME_MS) events.push(...tracker.update(t, hasFace));
  return events;
}

describe("FaceLostTracker", () => {
  it("emits nothing while the face stays", () => {
    expect(feed(new FaceLostTracker(), 0, 5000, true)).toEqual([]);
  });

  it("ignores a flicker shorter than the debounce, in both directions", () => {
    const tracker = new FaceLostTracker();
    expect(feed(tracker, 0, 1000, true)).toEqual([]);
    expect(feed(tracker, 1000, 1000 + FACE_LOST_DEBOUNCE_MS - FRAME_MS, false)).toEqual([]);
    expect(feed(tracker, 2000, 3000, true)).toEqual([]);
    // a confirmed loss, then a short return that does not count as regained
    const lost = feed(tracker, 3000, 4000, false);
    expect(lost.map((e) => e.type)).toEqual(["face_lost"]);
    expect(feed(tracker, 4000, 4000 + FACE_LOST_DEBOUNCE_MS - FRAME_MS, true)).toEqual([]);
    expect(feed(tracker, 4400, 4600, false)).toEqual([]);
    expect(tracker.update(4700, true)).toEqual([]);
  });

  it("emits face_lost once the face was absent for 500 ms", () => {
    const tracker = new FaceLostTracker();
    feed(tracker, 0, 1000, true);
    expect(tracker.update(1000, false)).toEqual([]);
    expect(tracker.update(1000 + FACE_LOST_DEBOUNCE_MS - 1, false)).toEqual([]);
    expect(tracker.update(1000 + FACE_LOST_DEBOUNCE_MS, false)).toEqual([
      { t: 1500, type: "face_lost", data: {} },
    ]);
    // not repeated
    expect(feed(tracker, 1600, 3000, false)).toEqual([]);
  });

  it("gives lost, banner and regained with the loss duration for a 3.2 s loss", () => {
    const tracker = new FaceLostTracker();
    feed(tracker, 0, 1000, true);
    const events: FaceLostEvent[] = [];
    let bannerAt: number | null = null;
    for (let t = 1000; t < 1000 + 3200; t += FRAME_MS) {
      events.push(...tracker.update(t, false));
      if (bannerAt === null && tracker.bannerVisible(t)) bannerAt = t;
    }
    expect(tracker.bannerVisible(4100)).toBe(true);
    expect(bannerAt).toBeGreaterThan(1000 + FACE_LOST_BANNER_MS);
    expect(bannerAt).toBeLessThanOrEqual(1000 + FACE_LOST_BANNER_MS + FRAME_MS);
    // the face is back from t = 4200
    for (let t = 4200; t < 5000; t += FRAME_MS) events.push(...tracker.update(t, true));
    expect(events.map((e) => e.type)).toEqual(["face_lost", "face_regained"]);
    const regained = events[1];
    if (regained?.type !== "face_regained") throw new Error("expected face_regained");
    expect(regained.data.lost_ms).toBeGreaterThan(3200 - FRAME_MS);
    expect(regained.data.lost_ms).toBeLessThan(3200 + FRAME_MS);
    expect(tracker.bannerVisible(5000)).toBe(false);
  });

  it("shows the banner only after more than 3 s and not for short losses", () => {
    const tracker = new FaceLostTracker();
    feed(tracker, 0, 1000, true);
    tracker.update(1000, false);
    tracker.update(1500, false);
    expect(tracker.bannerVisible(2000)).toBe(false);
    expect(tracker.bannerVisible(1000 + FACE_LOST_BANNER_MS)).toBe(false);
    expect(tracker.bannerVisible(1000 + FACE_LOST_BANNER_MS + 1)).toBe(true);
  });

  it("keeps the banner through a short return of the face", () => {
    const tracker = new FaceLostTracker();
    feed(tracker, 0, 100, true);
    feed(tracker, 100, 3500, false);
    expect(tracker.bannerVisible(3500)).toBe(true);
    tracker.update(3533, true);
    expect(tracker.bannerVisible(3533)).toBe(true);
  });

  it("can lose and regain the face repeatedly", () => {
    const tracker = new FaceLostTracker();
    const types: string[] = [];
    let t = 0;
    for (let i = 0; i < 3; i++) {
      for (const hasFace of [true, false]) {
        for (let k = 0; k < 30; k++, t += FRAME_MS) {
          types.push(...tracker.update(t, hasFace).map((e) => e.type));
        }
      }
    }
    for (let k = 0; k < 30; k++, t += FRAME_MS) {
      types.push(...tracker.update(t, true).map((e) => e.type));
    }
    expect(types).toEqual([
      "face_lost",
      "face_regained",
      "face_lost",
      "face_regained",
      "face_lost",
      "face_regained",
    ]);
  });

  it("does not report a loss when the first frames have no face for under 500 ms", () => {
    const tracker = new FaceLostTracker();
    expect(feed(tracker, 0, 400, false)).toEqual([]);
    expect(feed(tracker, 400, 800, true)).toEqual([]);
  });
});
