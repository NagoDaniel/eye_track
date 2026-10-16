import { describe, expect, it } from "vitest";
import faceFrames from "../../../shared/fixtures/face_frames.json";
import { CALIB_POST_CLICK_MS } from "../constants";
import { GATE_BLOCKS, GATE_REASONS } from "../eventLog";
import { frameFeatures } from "./features";
import {
  BLINK_OPENNESS_THRESHOLD,
  BLINK_SCORE_THRESHOLD,
  MAX_FACE_RATIO_SPREAD,
  MAX_HEAD_ANGLE_SPREAD_DEG,
  TRAIN_WINDOW_BEFORE_CLICK_MS,
  VECTOR_FACE_RATIO,
  VECTOR_LENGTH,
  VECTOR_PITCH,
  VECTOR_YAW,
  calibGateEventData,
  calibrationSignals,
  decideGate,
  featureVector,
  fitCalibration,
  isBlink,
  median,
  scoreValidation,
  skippedGateEventData,
  usableVectors,
} from "./quickCalib";
import type {
  CalibFrame,
  CalibrationSignals,
  DotSamples,
  GateMeasurements,
  GateThresholds,
  Viewport,
} from "./quickCalib";
import { predict } from "./ridge";

const VIEWPORT: Viewport = { width: 1600, height: 900 };
const DIAGONAL = Math.hypot(VIEWPORT.width, VIEWPORT.height);
const THRESHOLDS: GateThresholds = {
  max_mean_error_norm: 0.1,
  max_point_error_norm: 0.18,
  min_valid_frames_per_dot: 5,
  brightness_min: 0.25,
};
const CALM: CalibrationSignals = { meanBrightness: 0.5, faceRatioSpread: 0, headAngleSpreadDeg: 0 };

// --- Real MediaPipe frames (shared/fixtures/face_frames.json) ---

interface RealFrame {
  vector: number[];
  blink: boolean;
  blinkScore: number;
}
const REAL: RealFrame[] = faceFrames.frames.map((f) => {
  const features = frameFeatures(f.lm as [number, number, number][], f.matrix, faceFrames.aspect);
  return {
    vector: featureVector(features),
    blink: isBlink(f.blink_l, f.blink_r, features.openL, features.openR),
    blinkScore: Math.max(f.blink_l, f.blink_r),
  };
});
const OPEN = REAL.filter((f) => !f.blink);

describe("featureVector", () => {
  it("lists the iris, openness, head angles and face ratio of the frame in the documented order", () => {
    const f = faceFrames.frames[0];
    if (f === undefined) throw new Error("no fixture frame");
    const features = frameFeatures(f.lm as [number, number, number][], f.matrix, faceFrames.aspect);
    const vector = featureVector(features);
    expect(vector).toHaveLength(VECTOR_LENGTH);
    expect(vector.slice(0, 4)).toEqual([...features.irisL, ...features.irisR]);
    expect(vector[VECTOR_YAW]).toBe(features.yaw);
    expect(vector[VECTOR_PITCH]).toBe(features.pitch);
    expect(vector[VECTOR_FACE_RATIO]).toBe(features.faceRatio);
    expect(vector.every(Number.isFinite)).toBe(true);
  });
});

describe("isBlink", () => {
  it("is true for a blendshape above the score threshold in either eye", () => {
    expect(isBlink(BLINK_SCORE_THRESHOLD + 0.01, 0, 0.3, 0.3)).toBe(true);
    expect(isBlink(0, BLINK_SCORE_THRESHOLD + 0.01, 0.3, 0.3)).toBe(true);
    expect(isBlink(BLINK_SCORE_THRESHOLD, BLINK_SCORE_THRESHOLD, 0.3, 0.3)).toBe(false);
  });

  it("is true for an eye more closed than the openness threshold", () => {
    expect(isBlink(0, 0, BLINK_OPENNESS_THRESHOLD - 0.01, 0.3)).toBe(true);
    expect(isBlink(0, 0, 0.3, BLINK_OPENNESS_THRESHOLD - 0.01)).toBe(true);
    expect(isBlink(0, 0, BLINK_OPENNESS_THRESHOLD, BLINK_OPENNESS_THRESHOLD)).toBe(false);
  });

  it("flags every real frame with a blink blendshape and no real open-eye frame", () => {
    const blinking = REAL.filter((f) => f.blinkScore > BLINK_SCORE_THRESHOLD);
    expect(blinking.length).toBeGreaterThan(0);
    expect(blinking.every((f) => f.blink)).toBe(true);
    const clearlyOpen = REAL.filter((f) => f.blinkScore < 0.3);
    expect(clearlyOpen.length).toBeGreaterThan(20);
    expect(clearlyOpen.every((f) => !f.blink)).toBe(true);
  });
});

// --- Dots built from the real frames, with a known gaze mapping for the targets ---

const DOT_SPACING_MS = 2000;
const FRAMES_PER_DOT = 2;

/** The synthetic gaze: a pixel position that depends smoothly on the real iris position. */
function gaze(vectors: number[][]): [number, number] {
  const mean = (i: number): number =>
    vectors.reduce((s, v) => s + (v[i] as number), 0) / vectors.length;
  const iris = (mean(0) + mean(2)) / 2;
  const irisY = (mean(1) + mean(3)) / 2;
  return [VIEWPORT.width / 2 + 2000 * iris, VIEWPORT.height / 2 + 1500 * irisY];
}

/** A dot made of consecutive real frames inside its training window. */
function dotFrom(id: number, pair: RealFrame[]): DotSamples {
  const clickT = 1000 + id * DOT_SPACING_MS;
  const [x, y] = gaze(pair.map((f) => f.vector));
  const frames: CalibFrame[] = pair.map((f, i) => ({
    t: clickT - 300 + i * 100,
    face: true,
    blink: f.blink,
    vector: f.vector,
  }));
  return { id, x, y, clickT, frames };
}

// 18 pairs of consecutive open-eye frames; every third pair is a validation point (interpolation).
const PAIRS: RealFrame[][] = Array.from({ length: 18 }, (_, i) =>
  OPEN.slice(i * FRAMES_PER_DOT, (i + 1) * FRAMES_PER_DOT),
);
const CALIBRATION_DOTS = PAIRS.filter((_, i) => i % 3 !== 2).map((p, i) => dotFrom(i, p));
const VALIDATION_DOTS = PAIRS.filter((_, i) => i % 3 === 2).map((p, i) => dotFrom(100 + i, p));

describe("fitCalibration and scoreValidation on real frames", () => {
  it("counts the usable frames per dot in dot order", () => {
    const fit = fitCalibration(CALIBRATION_DOTS);
    expect(CALIBRATION_DOTS).toHaveLength(12);
    expect(fit.validFramesPerDot).toEqual(new Array(12).fill(FRAMES_PER_DOT));
    expect(fit.model).not.toBeNull();
  });

  it("scores real validation frames against the known mapping", () => {
    const fit = fitCalibration(CALIBRATION_DOTS);
    const score = scoreValidation(fit.model, VALIDATION_DOTS, VIEWPORT);
    expect(score.pointErrors).toHaveLength(VALIDATION_DOTS.length);
    expect(score.pointErrors.every((e) => e !== null && e < 0.12)).toBe(true);
    expect(score.meanErrorNorm).not.toBeNull();
    expect(score.meanErrorNorm as number).toBeLessThan(0.05);
  });

  it("measures the distance to the target in viewport diagonals", () => {
    const fit = fitCalibration(CALIBRATION_DOTS);
    const honest = scoreValidation(fit.model, VALIDATION_DOTS, VIEWPORT);
    const shiftPx = 0.2 * DIAGONAL;
    const moved = VALIDATION_DOTS.map((d) => ({ ...d, x: d.x + shiftPx }));
    const shifted = scoreValidation(fit.model, moved, VIEWPORT);
    shifted.pointErrors.forEach((error, i) => {
      // Moving the target by 0.2 diagonals along x changes the error by at most 0.2.
      expect(Math.abs((error as number) - (honest.pointErrors[i] as number))).toBeLessThanOrEqual(
        0.2 + 1e-9,
      );
      expect(error as number).toBeGreaterThan(0.1);
    });
  });

  it("ignores blinks, frames without a face and frames outside the window", () => {
    const blinkFrame = REAL.find((f) => f.blink);
    if (blinkFrame === undefined) throw new Error("the fixture has no blink");
    const base = CALIBRATION_DOTS[0] as DotSamples;
    const before = base.clickT - TRAIN_WINDOW_BEFORE_CLICK_MS - 1;
    const after = base.clickT + CALIB_POST_CLICK_MS + 1;
    const noisy: DotSamples = {
      ...base,
      frames: [
        ...base.frames,
        { t: base.clickT - 200, face: true, blink: true, vector: blinkFrame.vector },
        { t: base.clickT - 150, face: false, blink: false, vector: null },
        { t: before, face: true, blink: false, vector: OPEN[40]?.vector ?? null },
        { t: after, face: true, blink: false, vector: OPEN[41]?.vector ?? null },
      ],
    };
    const clean = fitCalibration(CALIBRATION_DOTS);
    const dirty = fitCalibration([noisy, ...CALIBRATION_DOTS.slice(1)]);
    expect(dirty.validFramesPerDot).toEqual(clean.validFramesPerDot);
    expect(dirty.model).toEqual(clean.model);
  });
});

describe("usableVectors", () => {
  const vector = (n: number): number[] => new Array<number>(VECTOR_LENGTH).fill(n);
  const frame = (t: number, over: Partial<CalibFrame> = {}): CalibFrame => ({
    t,
    face: true,
    blink: false,
    vector: vector(t),
    ...over,
  });

  it("takes the window from 700 ms before the click to 100 ms after it, both ends included", () => {
    expect(TRAIN_WINDOW_BEFORE_CLICK_MS).toBe(700);
    const click = 5000;
    const dot: DotSamples = {
      id: 1,
      x: 0,
      y: 0,
      clickT: click,
      frames: [
        frame(click - 701),
        frame(click - 700),
        frame(click),
        frame(click + CALIB_POST_CLICK_MS),
        frame(click + CALIB_POST_CLICK_MS + 1),
      ],
    };
    expect(usableVectors(dot).map((v) => v[0])).toEqual([
      click - 700,
      click,
      click + CALIB_POST_CLICK_MS,
    ]);
  });

  it("drops frames without a face, with a blink or without a vector", () => {
    const dot: DotSamples = {
      id: 1,
      x: 0,
      y: 0,
      clickT: 1000,
      frames: [
        frame(900),
        frame(910, { face: false }),
        frame(920, { blink: true }),
        frame(930, { vector: null }),
      ],
    };
    expect(usableVectors(dot)).toHaveLength(1);
  });
});

describe("fitCalibration without enough data", () => {
  it("gives no model when no dot has a usable frame, and still counts per dot", () => {
    const empty: DotSamples = { id: 0, x: 1, y: 1, clickT: 1000, frames: [] };
    expect(fitCalibration([empty, { ...empty, id: 1 }])).toEqual({
      model: null,
      validFramesPerDot: [0, 0],
    });
  });
});

describe("scoreValidation", () => {
  const model = fitCalibration(CALIBRATION_DOTS).model;
  const dotWith = (id: number, x: number, y: number, vectors: number[][]): DotSamples => ({
    id,
    x,
    y,
    clickT: 1000,
    frames: vectors.map((vector, i) => ({ t: 800 + i * 10, face: true, blink: false, vector })),
  });

  it("uses the median prediction of the point's frames", () => {
    if (model === null) throw new Error("no model");
    const vectors = [OPEN[0], OPEN[10], OPEN[20]].map((f) => (f as RealFrame).vector);
    const predictions = vectors.map((v) => predict(model, v));
    const mx = median(predictions.map((p) => p[0]));
    const my = median(predictions.map((p) => p[1]));
    const target = { x: 700, y: 300 };
    const score = scoreValidation(model, [dotWith(7, target.x, target.y, vectors)], VIEWPORT);
    expect(score.pointErrors[0]).toBeCloseTo(
      Math.hypot(mx - target.x, my - target.y) / DIAGONAL,
      12,
    );
    expect(score.meanErrorNorm).toBe(score.pointErrors[0]);
  });

  it("gives null for a point without usable frames and averages the others", () => {
    if (model === null) throw new Error("no model");
    const vectors = [(OPEN[0] as RealFrame).vector];
    const score = scoreValidation(
      model,
      [dotWith(1, 100, 100, vectors), dotWith(2, 100, 100, []), dotWith(3, 900, 500, vectors)],
      VIEWPORT,
    );
    expect(score.pointErrors[1]).toBeNull();
    const [a, , c] = score.pointErrors as [number, null, number];
    expect(score.meanErrorNorm).toBeCloseTo((a + c) / 2, 12);
  });

  it("has no mean when no point has an error, or when there is no model", () => {
    const vectors = [(OPEN[0] as RealFrame).vector];
    expect(scoreValidation(model, [dotWith(1, 0, 0, [])], VIEWPORT)).toEqual({
      pointErrors: [null],
      meanErrorNorm: null,
    });
    expect(scoreValidation(null, [dotWith(1, 0, 0, vectors)], VIEWPORT)).toEqual({
      pointErrors: [null],
      meanErrorNorm: null,
    });
  });

  it("rejects a viewport without area", () => {
    expect(() => scoreValidation(model, [], { width: 0, height: 0 })).toThrow(/viewport/);
  });
});

describe("median", () => {
  it("handles odd, even and empty input", () => {
    expect(median([3, 1, 2])).toBe(2);
    expect(median([4, 1, 3, 2])).toBe(2.5);
    expect(() => median([])).toThrow();
  });
});

// --- The gate decision ---

function measured(over: Partial<GateMeasurements> = {}): GateMeasurements {
  return {
    validFramesPerDot: new Array<number>(13).fill(20),
    pointErrors: [0.05, 0.06, 0.04, 0.07, 0.05],
    meanErrorNorm: 0.054,
    ...over,
  };
}

describe("decideGate", () => {
  const BAD = { pointErrors: [0.3], meanErrorNorm: 0.3 };

  it("passes a good calibration", () => {
    expect(decideGate(measured(), THRESHOLDS, CALM)).toEqual({ passed: true, reason: "ok" });
  });

  it("passes exactly at the limits", () => {
    const edge = measured({
      pointErrors: [0.18, 0.02],
      meanErrorNorm: 0.1,
      validFramesPerDot: [5, 5],
    });
    expect(decideGate(edge, THRESHOLDS, CALM).passed).toBe(true);
  });

  it("fails when one point is over the limit even if the mean is fine", () => {
    const bad = measured({ pointErrors: [0.19, 0.01, 0.01, 0.01, 0.01], meanErrorNorm: 0.046 });
    expect(decideGate(bad, THRESHOLDS, CALM)).toEqual({ passed: false, reason: "high_error" });
  });

  it("fails when the mean is over the limit even if no point is", () => {
    const bad = measured({ pointErrors: [0.12, 0.13, 0.12], meanErrorNorm: 0.123 });
    expect(decideGate(bad, THRESHOLDS, CALM)).toEqual({ passed: false, reason: "high_error" });
  });

  it("fails when a dot has too few usable frames", () => {
    const frames = new Array<number>(13).fill(20);
    frames[4] = THRESHOLDS.min_valid_frames_per_dot - 1;
    expect(decideGate(measured({ validFramesPerDot: frames }), THRESHOLDS, CALM)).toEqual({
      passed: false,
      reason: "few_face_frames",
    });
  });

  it("fails when a validation point has no error, and when none has", () => {
    const some = measured({ pointErrors: [0.05, null, 0.05], meanErrorNorm: 0.05 });
    expect(decideGate(some, THRESHOLDS, CALM)).toEqual({
      passed: false,
      reason: "few_face_frames",
    });
    const none = measured({ pointErrors: [null, null], meanErrorNorm: null });
    expect(decideGate(none, THRESHOLDS, CALM)).toEqual({
      passed: false,
      reason: "few_face_frames",
    });
  });

  it("names low brightness as the cause of a high error", () => {
    const dark = { ...CALM, meanBrightness: THRESHOLDS.brightness_min - 0.01 };
    expect(decideGate(measured(BAD), THRESHOLDS, dark).reason).toBe("low_brightness");
    const unknown = { ...CALM, meanBrightness: null };
    expect(decideGate(measured(BAD), THRESHOLDS, unknown).reason).toBe("high_error");
  });

  it("names a face-size change or head movement as the cause of a high error", () => {
    const closer = { ...CALM, faceRatioSpread: MAX_FACE_RATIO_SPREAD + 0.01 };
    const turning = { ...CALM, headAngleSpreadDeg: MAX_HEAD_ANGLE_SPREAD_DEG + 1 };
    expect(decideGate(measured(BAD), THRESHOLDS, closer).reason).toBe("head_or_distance_change");
    expect(decideGate(measured(BAD), THRESHOLDS, turning).reason).toBe("head_or_distance_change");
    const atLimit = {
      ...CALM,
      faceRatioSpread: MAX_FACE_RATIO_SPREAD,
      headAngleSpreadDeg: MAX_HEAD_ANGLE_SPREAD_DEG,
    };
    expect(decideGate(measured(BAD), THRESHOLDS, atLimit).reason).toBe("high_error");
  });

  it("checks the causes in order: frames, brightness, head", () => {
    const frames = new Array<number>(13).fill(20);
    frames[0] = 0;
    const everything = { meanBrightness: 0, faceRatioSpread: 1, headAngleSpreadDeg: 90 };
    const fewFrames = measured({ ...BAD, validFramesPerDot: frames });
    expect(decideGate(fewFrames, THRESHOLDS, everything).reason).toBe("few_face_frames");
    expect(decideGate(measured(BAD), THRESHOLDS, everything).reason).toBe("low_brightness");
    const bright = { ...everything, meanBrightness: 0.5 };
    expect(decideGate(measured(BAD), THRESHOLDS, bright).reason).toBe("head_or_distance_change");
  });

  it("reports ok exactly when it passes", () => {
    for (const m of [measured(), measured({ meanErrorNorm: 0.5 })]) {
      const d = decideGate(m, THRESHOLDS, CALM);
      expect(d.passed).toBe(d.reason === "ok");
    }
  });
});

describe("calibrationSignals", () => {
  const dotWithVector = (id: number, vector: number[]): DotSamples => ({
    id,
    x: 0,
    y: 0,
    clickT: 1000,
    frames: [{ t: 900, face: true, blink: false, vector }],
  });
  const vec = (faceRatio: number, yaw: number, pitch: number): number[] => {
    const v = new Array<number>(VECTOR_LENGTH).fill(0);
    v[VECTOR_FACE_RATIO] = faceRatio;
    v[VECTOR_YAW] = yaw;
    v[VECTOR_PITCH] = pitch;
    return v;
  };

  it("reports the spread of face ratio, yaw and pitch over the dots", () => {
    const vectors = [vec(0.1, 0, 0), vec(0.12, 4, -3), vec(0.11, -6, 9)];
    const signals = calibrationSignals(
      vectors.map((v, i) => dotWithVector(i, v)),
      0.4,
    );
    expect(signals.meanBrightness).toBe(0.4);
    expect(signals.faceRatioSpread).toBeCloseTo(0.02 / 0.11, 12);
    expect(signals.headAngleSpreadDeg).toBe(12);
  });

  it("is calm for a single dot or no frames", () => {
    expect(calibrationSignals([], null)).toEqual({
      meanBrightness: null,
      faceRatioSpread: 0,
      headAngleSpreadDeg: 0,
    });
    expect(calibrationSignals([dotWithVector(0, vec(0.1, 5, 5))], 0.5).headAngleSpreadDeg).toBe(0);
  });

  it("is within the limits for the steady real clip", () => {
    const signals = calibrationSignals(CALIBRATION_DOTS, 0.5);
    expect(signals.faceRatioSpread).toBeLessThan(MAX_FACE_RATIO_SPREAD);
    expect(signals.headAngleSpreadDeg).toBeLessThan(MAX_HEAD_ANGLE_SPREAD_DEG);
  });
});

describe("the gate on real frames", () => {
  const fit = fitCalibration(CALIBRATION_DOTS);
  const thresholds = { ...THRESHOLDS, min_valid_frames_per_dot: FRAMES_PER_DOT };

  it("passes when the validation dots lie where the mapping says", () => {
    const score = scoreValidation(fit.model, VALIDATION_DOTS, VIEWPORT);
    const m = { validFramesPerDot: fit.validFramesPerDot, ...score };
    const signals = calibrationSignals(CALIBRATION_DOTS, 0.5);
    expect(decideGate(m, thresholds, signals)).toEqual({ passed: true, reason: "ok" });
  });

  it("fails with high_error when the participant looked elsewhere", () => {
    const wrong = VALIDATION_DOTS.map((d) => ({ ...d, x: d.x + 0.4 * DIAGONAL }));
    const score = scoreValidation(fit.model, wrong, VIEWPORT);
    const m = { validFramesPerDot: fit.validFramesPerDot, ...score };
    const signals = calibrationSignals(CALIBRATION_DOTS, 0.5);
    expect(decideGate(m, thresholds, signals)).toEqual({ passed: false, reason: "high_error" });
  });

  it("fails with few_face_frames when the face is lost during the calibration", () => {
    const lost = CALIBRATION_DOTS.map((d, i) =>
      i === 3 ? { ...d, frames: d.frames.map((f) => ({ ...f, face: false, vector: null })) } : d,
    );
    const lostFit = fitCalibration(lost);
    expect(lostFit.validFramesPerDot[3]).toBe(0);
    const score = scoreValidation(lostFit.model, VALIDATION_DOTS, VIEWPORT);
    const m = { validFramesPerDot: lostFit.validFramesPerDot, ...score };
    expect(decideGate(m, thresholds, calibrationSignals(lost, 0.5)).reason).toBe("few_face_frames");
  });
});

describe("calib_gate_result event data", () => {
  const KEYS = [
    "attempt",
    "block",
    "mean_error_norm",
    "passed",
    "point_errors",
    "reason",
    "skipped",
    "valid_frames_per_dot",
  ];

  it("describes a decided gate", () => {
    const m = measured({ pointErrors: [0.05, null], meanErrorNorm: 0.05 });
    const decision = decideGate(m, THRESHOLDS, CALM);
    const data = calibGateEventData("before_video", 2, m, decision);
    expect(Object.keys(data).sort()).toEqual(KEYS);
    expect(data).toMatchObject({
      block: "before_video",
      attempt: 2,
      passed: false,
      skipped: false,
      mean_error_norm: 0.05,
      point_errors: [0.05, null],
      reason: "few_face_frames",
    });
    expect(data.valid_frames_per_dot).toEqual(m.validFramesPerDot);
  });

  it("describes a skipped gate", () => {
    const data = skippedGateEventData("between_videos", 1);
    expect(Object.keys(data).sort()).toEqual(KEYS);
    expect(data).toEqual({
      block: "between_videos",
      attempt: 1,
      passed: null,
      skipped: true,
      mean_error_norm: null,
      point_errors: [],
      valid_frames_per_dot: [],
      reason: "no_validation",
    });
    expect(GATE_BLOCKS).toContain(data.block);
    expect(GATE_REASONS).toContain(data.reason);
  });

  it("is JSON-serialisable without loss", () => {
    const m = measured();
    const data = calibGateEventData("after_last_video", 1, m, decideGate(m, THRESHOLDS, CALM));
    expect(JSON.parse(JSON.stringify(data))).toEqual(data);
  });
});
