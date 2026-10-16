import { describe, expect, it } from "vitest";

import faceFrames from "../../../shared/fixtures/face_frames.json";
import protocol from "../../../shared/protocol.json";
import type { StudyConfig } from "../config";
import { EventLog, type EventRecord } from "../eventLog";
import {
  CalibrationGate,
  DotTracker,
  FrameBuffer,
  type GateFrame,
  gateFrame,
} from "./calibrationGate";
import { frameFeatures } from "./features";
import type { Landmark } from "../records";

const VIEWPORT = { width: 1600, height: 900 };

// --- real MediaPipe frames: the open-eye ones, as the gate would see them ---

const REAL = faceFrames.frames.map((f) => {
  const features = frameFeatures(f.lm as Landmark[], f.matrix, faceFrames.aspect);
  return { features, blinkL: f.blink_l, blinkR: f.blink_r };
});
const OPEN = REAL.filter(
  (f) => f.blinkL < 0.3 && f.blinkR < 0.3 && f.features.openL > 0.2 && f.features.openR > 0.2,
);

function frameAt(t: number, real: (typeof REAL)[number], brightness = 0.5): GateFrame {
  return gateFrame(t, real.features, { blinkL: real.blinkL, blinkR: real.blinkR }, brightness);
}

/** The synthetic gaze: a pixel position that depends smoothly on the real iris position. */
function gazeOf(real: (typeof REAL)[number]): [number, number] {
  const iris = (real.features.irisL[0] + real.features.irisR[0]) / 2;
  const irisY = (real.features.irisL[1] + real.features.irisR[1]) / 2;
  return [VIEWPORT.width / 2 + 2000 * iris, VIEWPORT.height / 2 + 1500 * irisY];
}

// Config: the plan's defaults, except 2 usable frames per dot suffice (the fixture has 45 frames).
const CONFIG: StudyConfig = {
  ...(protocol.study_config_default as unknown as StudyConfig),
  calibration_gate: {
    ...(protocol.study_config_default.calibration_gate as StudyConfig["calibration_gate"]),
    min_valid_frames_per_dot: 2,
  },
};

interface Harness {
  gate: CalibrationGate;
  events: EventRecord[];
  frames: FrameBuffer;
  dots: DotTracker;
  now: () => number;
  /** Show and click `count` dots of one phase; each dot gets two real frames in its window. */
  run(phase: "calibration" | "validation", from: number, count: number, shift?: number): void;
  results(): Array<Record<string, unknown>>;
}

function harness(config: StudyConfig = CONFIG, forceFailure?: () => boolean): Harness {
  let t = 0;
  const events: EventRecord[] = [];
  const frames = new FrameBuffer();
  const dots = new DotTracker();
  const log = new EventLog(
    (r) => {
      events.push(r);
      dots.push(r);
    },
    () => t,
  );
  const gate = new CalibrationGate({
    config,
    log,
    frames,
    dots,
    viewport: () => VIEWPORT,
    forceFailure,
  });
  return {
    gate,
    events,
    frames,
    dots,
    now: () => t,
    run(phase, from, count, shift = 0) {
      for (let i = 0; i < count; i++) {
        const [a, b] = [OPEN[from + 2 * i], OPEN[from + 2 * i + 1]];
        if (a === undefined || b === undefined)
          throw new Error("not enough open frames in the fixture");
        const [x, y] = gazeOf(a);
        t += 100;
        log.log("calib_dot_show", { phase, id: i, x: x + shift, y });
        t += 1200;
        frames.push(frameAt(t - 200, a));
        frames.push(frameAt(t - 100, b));
        log.log("calib_dot_click", { phase, id: i });
        t += 100;
        log.log("calib_dot_hide", { phase, id: i });
      }
    },
    results: () => events.filter((e) => e.type === "calib_gate_result").map((e) => e.data),
  };
}

describe("DotTracker", () => {
  it("lists clicked dots in shown order and ignores a dot that was never clicked", () => {
    const h = harness();
    h.run("calibration", 0, 3);
    h.dots.push({
      t: 99_999,
      type: "calib_dot_show",
      data: { phase: "calibration", id: 3, x: 1, y: 2 },
    });
    expect(h.dots.clicked("calibration").map((d) => d.id)).toEqual([0, 1, 2]);
    expect(h.dots.clicked("validation")).toEqual([]);
  });

  it("uses the last show and click of a dot that restarted with the same id", () => {
    const dots = new DotTracker();
    const feed = (
      t: number,
      type: "calib_dot_show" | "calib_dot_click",
      data: Record<string, unknown>,
    ): void => dots.push({ t, type, data });
    feed(100, "calib_dot_show", { phase: "calibration", id: 0, x: 10, y: 20 });
    feed(500, "calib_dot_show", { phase: "calibration", id: 0, x: 10, y: 20 });
    feed(2000, "calib_dot_click", { phase: "calibration", id: 0 });
    expect(dots.clicked("calibration")).toEqual([
      { id: 0, x: 10, y: 20, showT: 500, clickT: 2000 },
    ]);
  });
});

describe("CalibrationGate on real frames", () => {
  it("passes a consistent attempt, logs it once and keeps the model", () => {
    const h = harness();
    h.gate.beginAttempt(0);
    h.run("calibration", 0, 12);
    h.run("validation", 24, 5);
    const decision = h.gate.evaluatePreVideo(1);
    expect(decision).toEqual({ passed: true, reason: "ok" });
    expect(h.gate.model).not.toBeNull();
    const [result, ...rest] = h.results();
    expect(rest).toEqual([]);
    expect(result).toMatchObject({
      block: "before_video",
      attempt: 1,
      passed: true,
      skipped: false,
      reason: "ok",
    });
    expect(result?.valid_frames_per_dot).toEqual(new Array(12).fill(2));
    expect((result?.point_errors as number[]).every((e) => e < 0.12)).toBe(true);
  });

  it("keeps a report of the last validation for the debug panel: targets and median predictions", () => {
    const h = harness();
    h.gate.beginAttempt(0);
    h.run("calibration", 0, 12);
    h.run("validation", 24, 5);
    h.gate.evaluatePreVideo(1);
    const report = h.gate.lastReport;
    expect(report).toMatchObject({ block: "before_video", attempt: 1 });
    expect(report?.points).toHaveLength(5);
    const diagonal = Math.hypot(VIEWPORT.width, VIEWPORT.height);
    report?.points.forEach((point, i) => {
      const error = Math.hypot(
        (point.prediction?.x ?? NaN) - point.target.x,
        (point.prediction?.y ?? NaN) - point.target.y,
      );
      expect(error / diagonal).toBeCloseTo(report.measured.pointErrors[i] ?? NaN, 9);
    });
  });

  it("fails when the validation targets are far from the prediction", () => {
    const h = harness();
    h.gate.beginAttempt(0);
    h.run("calibration", 0, 12);
    h.run("validation", 24, 5, 0.4 * Math.hypot(VIEWPORT.width, VIEWPORT.height));
    const decision = h.gate.evaluatePreVideo(1);
    expect(decision).toEqual({ passed: false, reason: "high_error" });
    expect(h.results()[0]).toMatchObject({ passed: false, reason: "high_error" });
  });

  it("a retry starts clean: the second attempt is scored alone and its model is kept", () => {
    const h = harness();
    h.gate.beginAttempt(h.now());
    h.run("calibration", 0, 12);
    h.run("validation", 24, 5, 800);
    expect(h.gate.evaluatePreVideo(1).passed).toBe(false);
    h.gate.beginAttempt(h.now());
    expect(h.dots.clicked("calibration")).toEqual([]);
    h.run("calibration", 0, 12);
    h.run("validation", 24, 5);
    expect(h.gate.evaluatePreVideo(2).passed).toBe(true);
    expect(h.results().map((r) => [r.attempt, r.passed])).toEqual([
      [1, false],
      [2, true],
    ]);
  });

  it("keeps the model of the last attempt when none passed, and of the last passed one otherwise", () => {
    const h = harness();
    h.gate.beginAttempt(0);
    h.run("calibration", 0, 12);
    h.run("validation", 24, 5, 800);
    h.gate.evaluatePreVideo(1);
    const first = h.gate.model;
    h.gate.beginAttempt(h.now());
    h.run("calibration", 2, 12);
    h.run("validation", 24, 5, 800);
    h.gate.evaluatePreVideo(2);
    expect(h.gate.model).not.toBe(first); // none passed: the last attempt's model
    h.gate.beginAttempt(h.now());
    h.run("calibration", 0, 12);
    h.run("validation", 24, 5);
    expect(h.gate.evaluatePreVideo(3).passed).toBe(true);
    const passed = h.gate.model;
    h.gate.beginAttempt(h.now());
    h.run("calibration", 4, 12);
    h.run("validation", 24, 5, 800);
    h.gate.evaluatePreVideo(4);
    expect(h.gate.model).toBe(passed); // a later failure does not replace a passed model
  });

  it("debug mode can force a failure", () => {
    const h = harness(CONFIG, () => true);
    h.gate.beginAttempt(0);
    h.run("calibration", 0, 12);
    h.run("validation", 24, 5);
    expect(h.gate.evaluatePreVideo(1)).toEqual({ passed: false, reason: "forced_debug" });
    expect(h.results()[0]).toMatchObject({ passed: false, reason: "forced_debug" });
  });

  it("with too few usable frames per dot reports few_face_frames", () => {
    const strict = harness({
      ...CONFIG,
      calibration_gate: { ...CONFIG.calibration_gate, min_valid_frames_per_dot: 5 },
    });
    strict.gate.beginAttempt(0);
    strict.run("calibration", 0, 12);
    strict.run("validation", 24, 5);
    expect(strict.gate.evaluatePreVideo(1)).toEqual({ passed: false, reason: "few_face_frames" });
  });

  it("a disabled gate passes, logs nothing and still keeps the model", () => {
    const h = harness({
      ...CONFIG,
      calibration_gate: { ...CONFIG.calibration_gate, enabled: false },
    });
    h.gate.beginAttempt(0);
    h.run("calibration", 0, 12);
    h.run("validation", 24, 5, 800);
    expect(h.gate.evaluatePreVideo(1)).toEqual({ passed: true, reason: "ok" });
    expect(h.results()).toEqual([]);
    expect(h.gate.model).not.toBeNull();
  });

  it("without a pre-video validation the gate is skipped and logged as such", () => {
    const h = harness();
    h.gate.beginAttempt(0);
    h.run("calibration", 0, 12);
    h.gate.skipPreVideo(1);
    expect(h.results()).toEqual([
      expect.objectContaining({
        block: "before_video",
        passed: null,
        skipped: true,
        reason: "no_validation",
        point_errors: [],
      }),
    ]);
    expect(h.gate.model).not.toBeNull();
  });

  it("later validation blocks are scored and logged but never decide anything", () => {
    const h = harness();
    h.gate.beginAttempt(0);
    h.run("calibration", 0, 12);
    h.run("validation", 24, 5);
    h.gate.evaluatePreVideo(1);
    h.dots.reset("validation");
    h.run("validation", 26, 5, 800); // drifted
    h.gate.scoreLater("between_videos");
    const later = h.results()[1];
    expect(later).toMatchObject({
      block: "between_videos",
      attempt: 1,
      passed: false,
      skipped: false,
    });
    h.dots.reset("validation");
    h.run("validation", 28, 5);
    h.gate.scoreLater("after_last_video");
    expect(h.results()[2]).toMatchObject({ block: "after_last_video" });
  });

  it("a later block without a model logs null errors instead of throwing", () => {
    const h = harness();
    h.run("validation", 24, 5);
    h.gate.scoreLater("after_last_video");
    expect(h.results()[0]).toMatchObject({ passed: false, mean_error_norm: null });
  });
});

describe("gateFrame and FrameBuffer", () => {
  it("a frame without a face has no vector and is not a blink", () => {
    expect(gateFrame(5, null, { blinkL: 0, blinkR: 0 }, null)).toEqual({
      t: 5,
      face: false,
      blink: false,
      vector: null,
      brightness: null,
    });
  });

  it("flags the real blink frames and not the open ones", () => {
    const blink = REAL.find((f) => f.blinkL > 0.5 && f.blinkR > 0.5);
    if (blink === undefined) throw new Error("the fixture has no blink");
    expect(frameAt(0, blink).blink).toBe(true);
    expect(OPEN.length).toBeGreaterThan(20);
    expect(OPEN.every((f) => !frameAt(0, f).blink)).toBe(true);
  });

  it("returns the frames in a time range and forgets old ones", () => {
    const buffer = new FrameBuffer();
    const real = OPEN[0];
    if (real === undefined) throw new Error("no frame");
    for (const t of [10, 20, 30, 40]) buffer.push(frameAt(t, real));
    expect(buffer.between(20, 30).map((f) => f.t)).toEqual([20, 30]);
    buffer.dropBefore(30);
    expect(buffer.between(0, 100).map((f) => f.t)).toEqual([30, 40]);
  });
});
