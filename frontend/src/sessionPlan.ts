// The order of the session after the participant has seen the instructions (PLAN §5.2 steps 6-12).

import type { StudyConfig } from "./config";

export type Step =
  | { kind: "fullscreen" }
  | { kind: "calibration" }
  | { kind: "validation" }
  | { kind: "playback"; videoId: string }
  | { kind: "upload" }
  | { kind: "done" };

/** fullscreen -> calibration -> [validation] -> per video: playback, [validation unless last] -> [final validation] -> upload -> done. */
export function sessionPlan(config: Pick<StudyConfig, "playlist" | "validation">): Step[] {
  const steps: Step[] = [{ kind: "fullscreen" }, { kind: "calibration" }];
  if (config.validation.before_video) steps.push({ kind: "validation" });
  const last = config.playlist.length - 1;
  for (const [i, videoId] of config.playlist.entries()) {
    steps.push({ kind: "playback", videoId });
    if (i < last && config.validation.between_videos) steps.push({ kind: "validation" });
  }
  if (config.validation.after_last_video) steps.push({ kind: "validation" });
  steps.push({ kind: "upload" }, { kind: "done" });
  return steps;
}
