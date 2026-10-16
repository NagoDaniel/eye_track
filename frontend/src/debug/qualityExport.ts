// Threshold tuning in debug mode: slider descriptors for the study config's `quality` block, the
// ordering rules the backend enforces, and a pasteable JSON export (PLAN §12.1, §4.1; DECISIONS
// "Milestone 2 scope" item 3). The defaults live in shared/protocol.json and are passed in.

import type { StudyConfig } from "../config";

export const QUALITY_KEYS = [
  "min_face_ratio_warn",
  "max_face_ratio_warn",
  "brightness_min",
  "brightness_max",
  "face_ratio_min",
  "face_ratio_max",
  "max_yaw_deg",
  "max_pitch_deg",
] as const;
export type QualityKey = (typeof QUALITY_KEYS)[number];

/** The `quality` block of the study config (PLAN §4.1; every key is numeric). */
export type QualityThresholds = StudyConfig["quality"];

export interface QualitySlider {
  key: QualityKey;
  label: string;
  min: number;
  max: number;
  step: number;
}

export const QUALITY_SLIDERS: readonly QualitySlider[] = [
  { key: "min_face_ratio_warn", label: "face scale warn below", min: 0.3, max: 1, step: 0.01 },
  { key: "max_face_ratio_warn", label: "face scale warn above", min: 1, max: 3, step: 0.01 },
  { key: "brightness_min", label: "brightness min", min: 0, max: 1, step: 0.01 },
  { key: "brightness_max", label: "brightness max", min: 0, max: 1, step: 0.01 },
  { key: "face_ratio_min", label: "face ratio min", min: 0.02, max: 0.4, step: 0.005 },
  { key: "face_ratio_max", label: "face ratio max", min: 0.02, max: 0.6, step: 0.005 },
  { key: "max_yaw_deg", label: "max yaw (deg)", min: 5, max: 60, step: 1 },
  { key: "max_pitch_deg", label: "max pitch (deg)", min: 5, max: 60, step: 1 },
];

/** Same ordering rules as the backend's QualityConfig: each lower bound is below its upper bound. */
export function isValidQuality(q: QualityThresholds): boolean {
  return (
    q.brightness_min < q.brightness_max &&
    q.face_ratio_min < q.face_ratio_max &&
    q.min_face_ratio_warn < q.max_face_ratio_warn
  );
}

/** A `"quality": { ... }` block with only the keys that differ from `defaults`, ready to paste. */
export function qualityToJson(quality: QualityThresholds, defaults: QualityThresholds): string {
  const changed: Partial<QualityThresholds> = {};
  for (const key of QUALITY_KEYS) {
    if (quality[key] !== defaults[key]) changed[key] = quality[key];
  }
  const body = JSON.stringify(changed, null, 2);
  return `"quality": ${body}`;
}
