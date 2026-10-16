import { describe, expect, it } from "vitest";

import fixture from "../../../shared/fixtures/face_frames.json";
import protocol from "../../../shared/protocol.json";
import type { StudyConfig } from "../config";
import {
  DEVICE_CHECK_STABLE_MS,
  DeviceCheckTracker,
  type QualityIssue,
  assessQuality,
  faceRatioWarning,
  median,
  qualityMessage,
} from "./quality";

const LIMITS: StudyConfig["quality"] = protocol.study_config_default.quality;
const OK = { faces: 1, brightness: 0.5, faceRatio: 0.07, yaw: 0, pitch: 0 };

describe("assessQuality", () => {
  it("reports nothing for the real clip with the default limits", () => {
    expect(fixture.frames.length).toBeGreaterThan(10);
    for (const frame of fixture.frames) {
      const e = frame.expected;
      expect(e.face_ratio).toBeGreaterThan(LIMITS.face_ratio_min);
      expect(e.face_ratio).toBeLessThan(LIMITS.face_ratio_max);
      expect(Math.abs(e.yaw)).toBeLessThan(LIMITS.max_yaw_deg);
      expect(Math.abs(e.pitch)).toBeLessThan(LIMITS.max_pitch_deg);
      const signals = {
        faces: 1,
        brightness: 0.5,
        faceRatio: e.face_ratio,
        yaw: e.yaw,
        pitch: e.pitch,
      };
      expect(assessQuality(signals, LIMITS)).toEqual([]);
    }
  });

  it("reports no face and nothing else", () => {
    const none = { faces: 0, brightness: 0.1, faceRatio: null, yaw: null, pitch: null };
    expect(assessQuality(none, LIMITS)).toEqual(["no_face"]);
  });

  it("reports several faces", () => {
    expect(assessQuality({ ...OK, faces: 2 }, LIMITS)).toEqual(["multiple_faces"]);
  });

  it("reports too dark and too bright at the limits", () => {
    const at = (brightness: number | null) => assessQuality({ ...OK, brightness }, LIMITS);
    expect(at(LIMITS.brightness_min - 0.01)).toEqual(["too_dark"]);
    expect(at(LIMITS.brightness_max + 0.01)).toEqual(["too_bright"]);
    expect(at(LIMITS.brightness_min)).toEqual([]);
    expect(at(LIMITS.brightness_max)).toEqual([]);
    expect(at(null)).toEqual([]);
  });

  it("reports too close and too far", () => {
    const at = (faceRatio: number) => assessQuality({ ...OK, faceRatio }, LIMITS);
    expect(at(LIMITS.face_ratio_max + 0.01)).toEqual(["too_close"]);
    expect(at(LIMITS.face_ratio_min - 0.01)).toEqual(["too_far"]);
    expect(at(LIMITS.face_ratio_min)).toEqual([]);
  });

  it("reports a head turned or tilted too far, once", () => {
    const at = (yaw: number, pitch: number) => assessQuality({ ...OK, yaw, pitch }, LIMITS);
    expect(at(-LIMITS.max_yaw_deg - 1, 0)).toEqual(["not_frontal"]);
    expect(at(0, LIMITS.max_pitch_deg + 1)).toEqual(["not_frontal"]);
    expect(at(30, 30)).toEqual(["not_frontal"]);
    expect(at(LIMITS.max_yaw_deg, -LIMITS.max_pitch_deg)).toEqual([]);
  });

  it("combines issues", () => {
    const signals = { faces: 2, brightness: 0.1, faceRatio: 0.3, yaw: 40, pitch: 0 };
    expect(assessQuality(signals, LIMITS)).toEqual([
      "multiple_faces",
      "too_dark",
      "too_close",
      "not_frontal",
    ]);
  });
});

describe("qualityMessage", () => {
  const issues: QualityIssue[] = [
    "no_face",
    "multiple_faces",
    "too_dark",
    "too_bright",
    "too_close",
    "too_far",
    "not_frontal",
  ];

  it("has a distinct message for every issue", () => {
    const messages = issues.map(qualityMessage);
    expect(new Set(messages).size).toBe(issues.length);
    messages.forEach((m) => expect(m.length).toBeGreaterThan(10));
  });

  it("tells the participant what to do", () => {
    expect(qualityMessage("too_far")).toBe("Move a little closer to the screen.");
    expect(qualityMessage("multiple_faces")).toBe("Only one person should be in view.");
    expect(qualityMessage("too_dark")).toMatch(/light/);
  });
});

describe("median", () => {
  it("handles odd, even and empty lists", () => {
    expect(median([3, 1, 2])).toBe(2);
    expect(median([4, 1, 2, 3])).toBe(2.5);
    expect(median([5])).toBe(5);
    expect(median([])).toBeNull();
  });
});

describe("faceRatioWarning", () => {
  it("uses the limits as multipliers of the reference", () => {
    const ref = 0.07;
    expect(faceRatioWarning(ref, ref, LIMITS)).toBeNull();
    expect(faceRatioWarning(ref * 0.81, ref, LIMITS)).toBeNull();
    expect(faceRatioWarning(ref * 1.24, ref, LIMITS)).toBeNull();
    expect(faceRatioWarning(ref * 0.79, ref, LIMITS)).toBe("too_far");
    expect(faceRatioWarning(ref * 1.26, ref, LIMITS)).toBe("too_close");
  });

  it("rejects a bad reference", () => {
    expect(() => faceRatioWarning(0.07, 0, LIMITS)).toThrow();
  });
});

describe("DeviceCheckTracker", () => {
  it("is ready after 3 s without any issue", () => {
    const tracker = new DeviceCheckTracker();
    for (let t = 0; t <= DEVICE_CHECK_STABLE_MS; t += 100) {
      expect(tracker.ready).toBe(false);
      tracker.update(1000 + t, [], 0.07);
    }
    expect(tracker.ready).toBe(true);
    expect(tracker.progress).toBe(1);
    expect(tracker.medianFaceRatio).toBe(0.07);
  });

  it("is not ready one millisecond before 3 s", () => {
    const tracker = new DeviceCheckTracker();
    tracker.update(0, []);
    tracker.update(DEVICE_CHECK_STABLE_MS - 1, []);
    expect(tracker.ready).toBe(false);
    expect(tracker.progress).toBeCloseTo(0.9997, 3);
    tracker.update(DEVICE_CHECK_STABLE_MS, []);
    expect(tracker.ready).toBe(true);
  });

  it("restarts the clock on any issue and forgets the ratios", () => {
    const tracker = new DeviceCheckTracker();
    tracker.update(0, [], 0.2);
    tracker.update(2500, [], 0.2);
    tracker.update(2600, ["too_dark"], 0.07);
    expect(tracker.issues).toEqual(["too_dark"]);
    expect(tracker.progress).toBe(0);
    expect(tracker.medianFaceRatio).toBeNull();
    tracker.update(2700, [], 0.07);
    tracker.update(2700 + 2900, [], 0.09);
    expect(tracker.ready).toBe(false);
    tracker.update(2700 + 3000, [], 0.08);
    expect(tracker.ready).toBe(true);
    expect(tracker.medianFaceRatio).toBe(0.08);
  });

  it("does not count a lost face as a stable period", () => {
    const tracker = new DeviceCheckTracker();
    tracker.update(0, []);
    tracker.update(2000, ["no_face"]);
    tracker.update(4000, ["no_face"]);
    expect(tracker.ready).toBe(false);
    expect(tracker.issues).toEqual(["no_face"]);
  });
});
