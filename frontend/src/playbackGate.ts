// Pure rules for playback (PLAN §5.2 end, §5.7): what to do when fullscreen changes, and what counts as a seek.

import { SEEK_TOLERANCE_S } from "./constants";

export interface FullscreenGateState {
  requireFullscreen: boolean;
  isFullscreen: boolean;
  /** True while the video is held back because fullscreen was left (or never entered). */
  pausedForFullscreen: boolean;
}

export type FullscreenAction = "pause" | "resume" | "none";

/** Pause when fullscreen is missing, resume only when it is back (and only if we were the ones who paused). */
export function fullscreenGate(state: FullscreenGateState): {
  action: FullscreenAction;
  pausedForFullscreen: boolean;
} {
  if (!state.requireFullscreen) return { action: "none", pausedForFullscreen: false };
  if (!state.isFullscreen && !state.pausedForFullscreen) return { action: "pause", pausedForFullscreen: true };
  if (state.isFullscreen && state.pausedForFullscreen) return { action: "resume", pausedForFullscreen: false };
  return { action: "none", pausedForFullscreen: state.pausedForFullscreen };
}

/** A jump of more than the tolerance (s) away from the last position seen during normal playback. */
export function isSeekAttempt(currentTime: number, lastGoodTime: number, toleranceS: number = SEEK_TOLERANCE_S): boolean {
  return Math.abs(currentTime - lastGoodTime) > toleranceS;
}
