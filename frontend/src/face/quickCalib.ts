// Browser calibration quality gate maths (PLAN §5.6): pick the training frames of each dot, fit the
// ridge model, score the validation dots and decide pass or fail. Pure functions, no DOM; the screens
// feed them frames from the live feature stream (PLAN §5.4) and log the result with
// `calibGateEventData`. The server's own fit (PLAN §8.6) stays authoritative.

import { CALIB_POST_CLICK_MS } from "../constants";
import type { EventDataMap, GateBlock, GateReason } from "../eventLog";
import type { FrameFeatures } from "./features";
import { MIN_RIDGE_SAMPLES, RIDGE_ALPHA, fitRidge, predict } from "./ridge";
import type { RidgeModel } from "./ridge";

/** PLAN §8.4: a blink is an eyeBlink blendshape above this in either eye ... */
export const BLINK_SCORE_THRESHOLD = 0.5;
/** ... or an eye openness (lid distance / eye width) below this. Starting value, tune on data: in the
 * fixture clip blinks measure 0.07 to 0.14 and open eyes 0.27 to 0.39. */
export const BLINK_OPENNESS_THRESHOLD = 0.15;
/** The training window of a dot starts this long before the click (PLAN §5.5); it ends
 * `CALIB_POST_CLICK_MS` after it. */
export const TRAIN_WINDOW_BEFORE_CLICK_MS = 700;
/** Head or distance change over the calibration: spread of the per-dot median face ratio, as a
 * fraction of its median (0.12 is about 6 cm at arm's length). Starting value, not in the plan. */
export const MAX_FACE_RATIO_SPREAD = 0.12;
/** Head movement over the calibration: spread (max - min) of the per-dot median yaw and pitch, in
 * degrees. Starting value, not in the plan; the fixture clip, in which the head turns visibly,
 * spreads 15 degrees in yaw. */
export const MAX_HEAD_ANGLE_SPREAD_DEG = 20;

/** Positions in the vector made by `featureVector`. */
export const VECTOR_YAW = 6;
export const VECTOR_PITCH = 7;
export const VECTOR_FACE_RATIO = 9;
export const VECTOR_LENGTH = 10;

/** [irisL x, irisL y, irisR x, irisR y, openL, openR, yaw, pitch, roll, faceRatio] (PLAN §8.4). */
export function featureVector(f: FrameFeatures): number[] {
  return [
    f.irisL[0],
    f.irisL[1],
    f.irisR[0],
    f.irisR[1],
    f.openL,
    f.openR,
    f.yaw,
    f.pitch,
    f.roll,
    f.faceRatio,
  ];
}

/** PLAN §8.4: blink = eyeBlink blendshape above the score threshold in either eye, or an eye more
 * closed than the openness threshold. */
export function isBlink(blinkL: number, blinkR: number, openL: number, openR: number): boolean {
  return (
    blinkL > BLINK_SCORE_THRESHOLD ||
    blinkR > BLINK_SCORE_THRESHOLD ||
    openL < BLINK_OPENNESS_THRESHOLD ||
    openR < BLINK_OPENNESS_THRESHOLD
  );
}

/** One frame of the live feature stream; `t` is the client clock in ms (PLAN §5.3). */
export interface CalibFrame {
  t: number;
  face: boolean;
  blink: boolean;
  /** `featureVector` of the frame; null when there is no face. */
  vector: number[] | null;
}

/** A calibration or validation dot with the frames seen around it. x and y are viewport pixels. */
export interface DotSamples {
  id: number;
  x: number;
  y: number;
  clickT: number;
  frames: CalibFrame[];
}
/** A validation dot has the same shape; its frames are never used for fitting. */
export type ValidationDotSamples = DotSamples;

export interface Viewport {
  width: number;
  height: number;
}

/** Vectors of the frames in the dot's training window that have a face and no blink. */
export function usableVectors(dot: DotSamples): number[][] {
  const from = dot.clickT - TRAIN_WINDOW_BEFORE_CLICK_MS;
  const to = dot.clickT + CALIB_POST_CLICK_MS;
  const vectors: number[][] = [];
  for (const frame of dot.frames) {
    if (frame.t < from || frame.t > to) continue;
    if (!frame.face || frame.blink || frame.vector === null) continue;
    vectors.push(frame.vector);
  }
  return vectors;
}

export interface CalibrationFit {
  /** null when the dots gave fewer than `MIN_RIDGE_SAMPLES` usable frames in total. */
  model: RidgeModel | null;
  /** Usable frames per calibration dot, in dot order. */
  validFramesPerDot: number[];
}

/** Fit the ridge model on the usable frames of all calibration dots; the target is the dot position. */
export function fitCalibration(dots: DotSamples[], alpha: number = RIDGE_ALPHA): CalibrationFit {
  const features: number[][] = [];
  const targets: Array<[number, number]> = [];
  const validFramesPerDot: number[] = [];
  for (const dot of dots) {
    const vectors = usableVectors(dot);
    validFramesPerDot.push(vectors.length);
    for (const vector of vectors) {
      features.push(vector);
      targets.push([dot.x, dot.y]);
    }
  }
  const model = features.length < MIN_RIDGE_SAMPLES ? null : fitRidge(features, targets, alpha);
  return { model, validFramesPerDot };
}

export function median(values: readonly number[]): number {
  if (values.length === 0) throw new Error("median of no values");
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  const upper = sorted[middle] as number;
  return sorted.length % 2 === 1 ? upper : ((sorted[middle - 1] as number) + upper) / 2;
}

export interface ValidationScore {
  /** Per validation point, in order; null when the point had no usable frame (or there is no model). */
  pointErrors: Array<number | null>;
  /** Mean over the points that have an error; null when none has. */
  meanErrorNorm: number | null;
}

/** Per validation point: distance between the median prediction and the target, divided by the
 * viewport diagonal (PLAN §5.6 step 4). */
export function scoreValidation(
  model: RidgeModel | null,
  dots: ValidationDotSamples[],
  viewport: Viewport,
): ValidationScore {
  const diagonal = Math.hypot(viewport.width, viewport.height);
  if (!(diagonal > 0)) throw new Error("the viewport has no area");
  const pointErrors = dots.map((dot): number | null => {
    const vectors = usableVectors(dot);
    if (model === null || vectors.length === 0) return null;
    const predictions = vectors.map((vector) => predict(model, vector));
    const x = median(predictions.map((p) => p[0]));
    const y = median(predictions.map((p) => p[1]));
    return Math.hypot(x - dot.x, y - dot.y) / diagonal;
  });
  const present = pointErrors.filter((error): error is number => error !== null);
  const meanErrorNorm =
    present.length === 0 ? null : present.reduce((sum, e) => sum + e, 0) / present.length;
  return { pointErrors, meanErrorNorm };
}

/** What the gate measured: `fitCalibration(...).validFramesPerDot` and `scoreValidation(...)`. */
export interface GateMeasurements extends ValidationScore {
  validFramesPerDot: number[];
}

/** Limits: `calibration_gate` of the study config plus `quality.brightness_min`. */
export interface GateThresholds {
  max_mean_error_norm: number;
  max_point_error_norm: number;
  min_valid_frames_per_dot: number;
  brightness_min: number;
}

/** Quality signals over the calibration, used to name the likely cause of a failure. */
export interface CalibrationSignals {
  /** Mean face-region brightness over the calibration; null when unknown. */
  meanBrightness: number | null;
  /** (max - min) / median of the per-dot median face ratio. */
  faceRatioSpread: number;
  /** Larger of the yaw and pitch spreads (max - min of the per-dot medians), degrees. */
  headAngleSpreadDeg: number;
}

function spread(values: readonly number[]): number {
  return values.length < 2 ? 0 : Math.max(...values) - Math.min(...values);
}

/** Face-size and head-angle spread over the usable frames of the calibration dots. */
export function calibrationSignals(
  dots: DotSamples[],
  meanBrightness: number | null,
): CalibrationSignals {
  const medians = (index: number): number[] =>
    dots
      .map(usableVectors)
      .filter((vectors) => vectors.length > 0)
      .map((vectors) => median(vectors.map((vector) => vector[index] as number)));
  const ratios = medians(VECTOR_FACE_RATIO);
  const ratioMedian = ratios.length === 0 ? 0 : median(ratios);
  return {
    meanBrightness,
    faceRatioSpread: ratioMedian > 0 ? spread(ratios) / ratioMedian : 0,
    headAngleSpreadDeg: Math.max(spread(medians(VECTOR_YAW)), spread(medians(VECTOR_PITCH))),
  };
}

export interface GateDecision {
  passed: boolean;
  reason: GateReason;
}

/** PLAN §5.6 step 5. Passes when the mean error and every point error are within their limits and every
 * calibration dot has enough usable frames. On failure `reason` is the most likely cause, checked in
 * this order: too few usable frames (a dot, or a validation point, without them), low brightness, head
 * or distance change, otherwise a high error. */
export function decideGate(
  measured: GateMeasurements,
  thresholds: GateThresholds,
  signals: CalibrationSignals,
): GateDecision {
  const { meanErrorNorm, pointErrors, validFramesPerDot } = measured;
  const enoughFrames = validFramesPerDot.every((n) => n >= thresholds.min_valid_frames_per_dot);
  const pointsMeasured = pointErrors.every((e) => e !== null);
  const errorsOk =
    meanErrorNorm !== null &&
    meanErrorNorm <= thresholds.max_mean_error_norm &&
    pointErrors.every((e) => e !== null && e <= thresholds.max_point_error_norm);
  if (enoughFrames && errorsOk) return { passed: true, reason: "ok" };
  if (!enoughFrames || !pointsMeasured) return { passed: false, reason: "few_face_frames" };
  if (signals.meanBrightness !== null && signals.meanBrightness < thresholds.brightness_min) {
    return { passed: false, reason: "low_brightness" };
  }
  if (
    signals.faceRatioSpread > MAX_FACE_RATIO_SPREAD ||
    signals.headAngleSpreadDeg > MAX_HEAD_ANGLE_SPREAD_DEG
  ) {
    return { passed: false, reason: "head_or_distance_change" };
  }
  return { passed: false, reason: "high_error" };
}

export type CalibGateResultData = EventDataMap["calib_gate_result"];

/** Data of the `calib_gate_result` event (PLAN §5.6 Logging) for a decided gate. */
export function calibGateEventData(
  block: GateBlock,
  attempt: number,
  measured: GateMeasurements,
  decision: GateDecision,
): CalibGateResultData {
  return {
    block,
    attempt,
    passed: decision.passed,
    skipped: false,
    mean_error_norm: measured.meanErrorNorm,
    point_errors: measured.pointErrors,
    valid_frames_per_dot: measured.validFramesPerDot,
    reason: decision.reason,
  };
}

/** Data of the `calib_gate_result` event when the gate did not run (debug skip of the validation). */
export function skippedGateEventData(
  block: GateBlock,
  attempt: number,
  validFramesPerDot: number[] = [],
): CalibGateResultData {
  return {
    block,
    attempt,
    passed: null,
    skipped: true,
    mean_error_norm: null,
    point_errors: [],
    valid_frames_per_dot: validFramesPerDot,
    reason: "no_validation",
  };
}
