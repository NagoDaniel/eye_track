// Device-check quality rules (PLAN §5.2 step 4) and the in-session face-size warning (PLAN §5.7).
// Pure: signals in, issues and messages out.

import type { StudyConfig } from "../config";

/** The face must be present with no issue for this long before the device check passes. */
export const DEVICE_CHECK_STABLE_MS = 3000;

export type QualityIssue =
  | "no_face"
  | "multiple_faces"
  | "too_dark"
  | "too_bright"
  | "too_close"
  | "too_far"
  | "not_frontal";

export type QualityLimits = StudyConfig["quality"];

export interface QualitySignals {
  faces: number;
  /** Mean luminance of the face region, 0 to 1; null when not measured. */
  brightness: number | null;
  /** Interocular distance over frame width; null without a face. */
  faceRatio: number | null;
  /** Head angles in degrees; null without a face. */
  yaw: number | null;
  pitch: number | null;
}

/**
 * The problems with the current frame, in a fixed order. Without a face only `no_face` is given
 * (nothing else can be measured). Several faces add `multiple_faces` to the issues of the first.
 */
export function assessQuality(signals: QualitySignals, limits: QualityLimits): QualityIssue[] {
  if (signals.faces < 1) return ["no_face"];
  const issues: QualityIssue[] = [];
  if (signals.faces > 1) issues.push("multiple_faces");
  const { brightness, faceRatio, yaw, pitch } = signals;
  if (brightness !== null) {
    if (brightness < limits.brightness_min) issues.push("too_dark");
    if (brightness > limits.brightness_max) issues.push("too_bright");
  }
  if (faceRatio !== null) {
    if (faceRatio > limits.face_ratio_max) issues.push("too_close");
    if (faceRatio < limits.face_ratio_min) issues.push("too_far");
  }
  const yawOff = yaw !== null && Math.abs(yaw) > limits.max_yaw_deg;
  const pitchOff = pitch !== null && Math.abs(pitch) > limits.max_pitch_deg;
  if (yawOff || pitchOff) issues.push("not_frontal");
  return issues;
}

const MESSAGES: Record<QualityIssue, string> = {
  no_face: "We cannot see your face. Sit in front of the camera and look at the screen.",
  multiple_faces: "Only one person should be in view.",
  too_dark: "Your face is too dark. Turn on a light in front of you and avoid windows behind you.",
  too_bright: "Your face is too bright. Dim the light in front of you or move away from it.",
  too_close: "Move a little further from the screen.",
  too_far: "Move a little closer to the screen.",
  not_frontal: "Look at the screen and face the camera straight on.",
};

/** A short, non-technical, actionable message for the participant. */
export function qualityMessage(issue: QualityIssue): string {
  return MESSAGES[issue];
}

/** Median of the values; null for an empty list. */
export function median(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  const upper = sorted[middle] ?? Number.NaN;
  if (sorted.length % 2 === 1) return upper;
  return ((sorted[middle - 1] ?? Number.NaN) + upper) / 2;
}

/**
 * Compares the face size now with the reference (the median face ratio of the device check).
 * `min_face_ratio_warn` and `max_face_ratio_warn` are multipliers of the reference: a smaller face
 * means the participant moved away, a larger one that they came closer.
 */
export function faceRatioWarning(
  current: number,
  reference: number,
  limits: Pick<QualityLimits, "min_face_ratio_warn" | "max_face_ratio_warn">,
): "too_close" | "too_far" | null {
  if (!(reference > 0)) throw new Error("the reference face ratio must be positive");
  const share = current / reference;
  if (share < limits.min_face_ratio_warn) return "too_far";
  if (share > limits.max_face_ratio_warn) return "too_close";
  return null;
}

/**
 * Tells when the device check has passed: a face with no issue for DEVICE_CHECK_STABLE_MS in a
 * row. Any issue restarts the clock. `ready` follows the current state (it goes false again if an
 * issue appears), so the caller should move on as soon as it sees it true.
 */
export class DeviceCheckTracker {
  private stableSince: number | null = null;
  private lastT = 0;
  private currentIssues: QualityIssue[] = [];
  private ratios: number[] = [];

  /** One sample per processed frame: its time, its issues and (optionally) its face ratio. */
  update(t: number, issues: readonly QualityIssue[], faceRatio: number | null = null): void {
    this.lastT = t;
    this.currentIssues = [...issues];
    if (issues.length > 0) {
      this.stableSince = null;
      this.ratios = [];
      return;
    }
    this.stableSince ??= t;
    if (faceRatio !== null) this.ratios.push(faceRatio);
  }

  /** True when the last sample ended a stable period of at least DEVICE_CHECK_STABLE_MS. */
  get ready(): boolean {
    return this.stableSince !== null && this.lastT - this.stableSince >= DEVICE_CHECK_STABLE_MS;
  }

  /** Progress of the stable period, 0 to 1 (for a progress bar). */
  get progress(): number {
    if (this.stableSince === null) return 0;
    return Math.min(1, (this.lastT - this.stableSince) / DEVICE_CHECK_STABLE_MS);
  }

  /** The issues of the last sample. */
  get issues(): readonly QualityIssue[] {
    return this.currentIssues;
  }

  /** Median face ratio of the current stable period; null before any sample with a ratio. */
  get medianFaceRatio(): number | null {
    return median(this.ratios);
  }
}
