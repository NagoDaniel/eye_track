// The order of the session after the participant has seen the instructions (PLAN §5.2 steps 6-12).

import type { StudyConfig } from "./config";

export type Step =
  | { kind: "fullscreen" }
  /** Calibration, then (if the study has it) the pre-video validation and the calibration gate with its retries. */
  | { kind: "calibration" }
  /** A validation block between or after the videos: scored and logged, never blocking (PLAN §5.6). */
  | { kind: "validation"; block: "between_videos" | "after_last_video" }
  | { kind: "playback"; videoId: string }
  | { kind: "upload" }
  | { kind: "done" };

/**
 * fullscreen -> calibration (+ [validation] + gate) -> per video: playback, [validation unless last]
 * -> [final validation] -> upload -> done.
 */
export function sessionPlan(config: Pick<StudyConfig, "playlist" | "validation">): Step[] {
  const steps: Step[] = [{ kind: "fullscreen" }, { kind: "calibration" }];
  const last = config.playlist.length - 1;
  for (const [i, videoId] of config.playlist.entries()) {
    steps.push({ kind: "playback", videoId });
    if (i < last && config.validation.between_videos) {
      steps.push({ kind: "validation", block: "between_videos" });
    }
  }
  if (config.validation.after_last_video) {
    steps.push({ kind: "validation", block: "after_last_video" });
  }
  steps.push({ kind: "upload" }, { kind: "done" });
  return steps;
}
