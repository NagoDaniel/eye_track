// The in-browser calibration quality gate (PLAN §5.6): collects the live frames and the dot clicks,
// fits the quick ridge model on a calibration attempt, scores the validation block, decides, and logs
// `calib_gate_result`. The maths are in quickCalib.ts / ridge.ts; this file keeps the state.

import type { StudyConfig } from "../config";
import { CALIB_POST_CLICK_MS } from "../constants";
import type { EventLog, EventRecord, GateBlock, Size } from "../eventLog";
import type { FrameFeatures } from "./features";
import {
  type CalibFrame,
  type DotSamples,
  type GateDecision,
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
  type GateMeasurements,
  type ModelChoice,
} from "./quickCalib";
import { type RidgeModel, predict } from "./ridge";

/** A processed webcam frame as the gate needs it. */
export interface GateFrame extends CalibFrame {
  /** Mean luminance of the face region, 0 to 1; null without a face. */
  brightness: number | null;
}

/** The gate's view of one live frame: features of a face (or null) plus the blink scores. */
export function gateFrame(
  t: number,
  features: FrameFeatures | null,
  blendshapes: { blinkL: number; blinkR: number },
  brightness: number | null,
): GateFrame {
  if (features === null) return { t, face: false, blink: false, vector: null, brightness: null };
  return {
    t,
    face: true,
    blink: isBlink(blendshapes.blinkL, blendshapes.blinkR, features.openL, features.openR),
    vector: featureVector(features),
    brightness,
  };
}

/** Frames of the last seconds; the gate asks for the ones around each dot. */
export class FrameBuffer {
  private frames: GateFrame[] = [];

  push(frame: GateFrame): void {
    this.frames.push(frame);
  }

  between(fromT: number, toT: number): GateFrame[] {
    return this.frames.filter((f) => f.t >= fromT && f.t <= toT);
  }

  /** Forget frames before `t` (called when a new attempt starts, so memory stays small). */
  dropBefore(t: number): void {
    this.frames = this.frames.filter((f) => f.t >= t);
  }
}

interface DotRecord {
  id: number;
  x: number;
  y: number;
  showT: number;
  clickT: number | null;
}

/**
 * Remembers the dots of the current attempt from the event stream. A dot that was interrupted and
 * restarted (fullscreen left) is shown again with the same id: the LAST show and click count.
 */
export class DotTracker {
  private readonly dots = {
    calibration: new Map<number, DotRecord>(),
    validation: new Map<number, DotRecord>(),
  };

  /** The EventLog sink side: feed every logged event. */
  readonly push = (record: EventRecord): void => {
    if (record.type === "calib_dot_show") {
      const d = record.data as {
        phase: "calibration" | "validation";
        id: number;
        x: number;
        y: number;
      };
      this.dots[d.phase].set(d.id, { id: d.id, x: d.x, y: d.y, showT: record.t, clickT: null });
    } else if (record.type === "calib_dot_click") {
      const d = record.data as { phase: "calibration" | "validation"; id: number };
      const dot = this.dots[d.phase].get(d.id);
      if (dot !== undefined) dot.clickT = record.t;
    }
  };

  reset(phase: "calibration" | "validation"): void {
    this.dots[phase].clear();
  }

  /** The clicked dots in the order they were shown (ids are the position in that order). */
  clicked(phase: "calibration" | "validation"): Array<DotRecord & { clickT: number }> {
    return [...this.dots[phase].values()]
      .filter((d): d is DotRecord & { clickT: number } => d.clickT !== null)
      .sort((a, b) => a.id - b.id);
  }
}

export interface GateDeps {
  config: StudyConfig;
  log: EventLog;
  frames: FrameBuffer;
  dots: DotTracker;
  viewport: () => Size;
  /** Debug mode `F`: make the next gate decision a failure (PLAN §12.1). */
  forceFailure?: () => boolean;
}

/** A scored validation block as the debug panel shows it (PLAN §12.1). */
export interface GateReport {
  block: GateBlock;
  attempt: number;
  decision: GateDecision;
  measured: GateMeasurements;
  /** The model the calibration chose by cross-validation (null without a model). */
  choice: ModelChoice | null;
  /** Per validation point: the target and the median prediction, in viewport pixels. */
  points: Array<{ target: { x: number; y: number }; prediction: { x: number; y: number } | null }>;
}

export class CalibrationGate {
  /** The last scored validation block (null before the first). */
  lastReport: GateReport | null = null;
  /** The model of the last passed attempt, or of the last attempt if none passed. */
  model: RidgeModel | null = null;
  /** The choice that belongs to `model`. */
  private choice: ModelChoice | null = null;
  private passedOnce = false;

  constructor(private readonly deps: GateDeps) {}

  get enabled(): boolean {
    return this.deps.config.calibration_gate.enabled;
  }

  /** Start a new calibration attempt: forget the dots and frames of the previous one. */
  beginAttempt(t: number): void {
    this.deps.dots.reset("calibration");
    this.deps.dots.reset("validation");
    this.deps.frames.dropBefore(t);
  }

  private report(
    block: GateBlock,
    attempt: number,
    decision: GateDecision,
    measured: GateMeasurements,
    model: RidgeModel | null,
    choice: ModelChoice | null,
    validation: DotSamples[],
  ): void {
    this.lastReport = {
      block,
      attempt,
      decision,
      measured,
      choice,
      points: validation.map((dot) => {
        const vectors = usableVectors(dot);
        if (model === null || vectors.length === 0) {
          return { target: { x: dot.x, y: dot.y }, prediction: null };
        }
        const predictions = vectors.map((v) => predict(model, v));
        return {
          target: { x: dot.x, y: dot.y },
          prediction: {
            x: median(predictions.map((q) => q[0])),
            y: median(predictions.map((q) => q[1])),
          },
        };
      }),
    };
  }

  private samples(phase: "calibration" | "validation"): {
    dots: DotSamples[];
    brightness: number[];
  } {
    const brightness: number[] = [];
    const dots = this.deps.dots.clicked(phase).map((d): DotSamples => {
      const frames = this.deps.frames.between(d.showT, d.clickT + CALIB_POST_CLICK_MS);
      for (const f of frames) if (f.brightness !== null) brightness.push(f.brightness);
      return { id: d.id, x: d.x, y: d.y, clickT: d.clickT, frames };
    });
    return { dots, brightness };
  }

  /**
   * Score the pre-video validation of `attempt`, decide, log `calib_gate_result` and keep the model.
   * With the gate disabled nothing is logged and the attempt counts as passed (the model is still
   * kept for the debug gaze dot).
   */
  evaluatePreVideo(attempt: number): GateDecision {
    const { config } = this.deps;
    const calibration = this.samples("calibration");
    const fit = fitCalibration(calibration.dots);
    const validation = this.samples("validation");
    const scored = scoreValidation(fit.model, validation.dots, this.deps.viewport());
    const measured = { ...scored, validFramesPerDot: fit.validFramesPerDot };
    const meanBrightness =
      calibration.brightness.length === 0
        ? null
        : calibration.brightness.reduce((a, b) => a + b, 0) / calibration.brightness.length;
    let decision = decideGate(
      measured,
      { ...config.calibration_gate, brightness_min: config.quality.brightness_min },
      calibrationSignals(calibration.dots, meanBrightness),
    );
    if (this.deps.forceFailure?.() === true) decision = { passed: false, reason: "forced_debug" };
    if (decision.passed || !this.passedOnce) {
      this.model = fit.model;
      this.choice = fit.choice;
    }
    if (decision.passed) this.passedOnce = true;
    this.report(
      "before_video",
      attempt,
      decision,
      measured,
      fit.model,
      fit.choice,
      validation.dots,
    );
    if (!this.enabled) return { passed: true, reason: "ok" };
    this.deps.log.log(
      "calib_gate_result",
      calibGateEventData("before_video", attempt, measured, decision),
    );
    return decision;
  }

  /** `validation.before_video` is false: the gate cannot be scored (DECISIONS, milestone 2 item 8). */
  skipPreVideo(attempt: number): void {
    const calibration = this.samples("calibration");
    const fit = fitCalibration(calibration.dots);
    this.model = fit.model;
    this.choice = fit.choice;
    if (this.enabled) {
      this.deps.log.log(
        "calib_gate_result",
        skippedGateEventData("before_video", attempt, fit.validFramesPerDot),
      );
    }
  }

  /** A later validation block: scored with the kept model and logged, never blocking (PLAN §5.6). */
  scoreLater(block: Exclude<GateBlock, "before_video">): void {
    if (!this.enabled) return;
    const { config } = this.deps;
    const validation = this.samples("validation");
    const scored = scoreValidation(this.model, validation.dots, this.deps.viewport());
    const measured = { ...scored, validFramesPerDot: [] };
    const decision = decideGate(
      measured,
      {
        ...config.calibration_gate,
        min_valid_frames_per_dot: 0,
        brightness_min: config.quality.brightness_min,
      },
      { meanBrightness: null, faceRatioSpread: 0, headAngleSpreadDeg: 0 },
    );
    this.report(block, 1, decision, measured, this.model, this.choice, validation.dots);
    this.deps.log.log("calib_gate_result", calibGateEventData(block, 1, measured, decision));
  }
}
