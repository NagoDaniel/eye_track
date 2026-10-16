// How often the face model runs (PLAN §5.4 and DECISIONS "Milestone 2 scope" item 10): at the
// stimulus video's frame rate (capped at the camera's), lowered when the stimulus video drops
// frames, because playback smoothness has priority. Pure: time and counters in, a rate out.
//
// All numbers below are starting values, to be tuned on real laptops (PLAN §14, pilot).

/** Window over which dropped frames are counted. */
export const RATE_WINDOW_MS = 2000;
/** Dropped / (shown + dropped) above this in the window lowers the rate. */
export const RATE_DROP_SHARE = 0.05;
/** The rate is multiplied by this when lowering (at most one step per window). */
export const RATE_STEP_DOWN = 0.75;
/** Time with no dropped frame before the rate is raised again. */
export const RATE_RECOVER_MS = 5000;
/** The rate is multiplied by this when raising (never above the target). */
export const RATE_STEP_UP = 1.25;
/** The rate never goes below this (frames per second). */
export const RATE_MIN_FPS = 8;
/** A frame may come this share of an interval early (camera timing jitter) and still pass. */
export const GATE_TOLERANCE_SHARE = 0.1;

/**
 * The target face model rate: the stimulus video's fps capped at the camera's. Without a video
 * (calibration, validation) it is the camera's fps.
 */
export function targetFps(videoFps: number | null, cameraFps: number): number {
  if (!(cameraFps > 0)) throw new Error("the camera fps must be positive");
  if (videoFps === null || !(videoFps > 0)) return cameraFps;
  return Math.min(videoFps, cameraFps);
}

export interface AdaptiveRateOptions {
  targetFps: number;
  minFps?: number;
}

interface Batch {
  t: number;
  seen: number;
  dropped: number;
}

export class AdaptiveRate {
  private target: number;
  private readonly minFps: number;
  private fps: number;
  private window: Batch[] = [];
  /** Time of the last rate change, of the last dropped frame, or of the first report. */
  private quietSince: number | null = null;
  private lastStepDownT = Number.NEGATIVE_INFINITY;

  constructor(options: AdaptiveRateOptions) {
    if (!(options.targetFps > 0)) throw new Error("the target fps must be positive");
    this.minFps = options.minFps ?? RATE_MIN_FPS;
    if (!(this.minFps > 0)) throw new Error("the minimum fps must be positive");
    this.target = options.targetFps;
    this.fps = options.targetFps;
  }

  /** Face model rate now, frames per second. */
  currentFps(): number {
    return this.fps;
  }

  /** A new target (the video changed): the rate is kept if it is below it, else lowered to it. */
  setTarget(targetFps: number): void {
    if (!(targetFps > 0)) throw new Error("the target fps must be positive");
    this.target = targetFps;
    this.fps = Math.min(this.fps, targetFps);
  }

  /** The floor in force: the minimum, but never above the target. */
  private floor(): number {
    return Math.min(this.minFps, this.target);
  }

  /** One stimulus frame batch: `framesSeen` presented, `droppedFrames` missed, at time `t`. */
  report(t: number, framesSeen: number, droppedFrames: number): void {
    if (framesSeen < 0 || droppedFrames < 0) throw new Error("frame counts cannot be negative");
    this.quietSince ??= t;
    this.window.push({ t, seen: framesSeen, dropped: droppedFrames });
    this.window = this.window.filter((batch) => t - batch.t <= RATE_WINDOW_MS);
    if (droppedFrames > 0) this.quietSince = t;

    const seen = this.window.reduce((sum, batch) => sum + batch.seen, 0);
    const dropped = this.window.reduce((sum, batch) => sum + batch.dropped, 0);
    const total = seen + dropped;
    if (
      total > 0 &&
      dropped / total > RATE_DROP_SHARE &&
      t - this.lastStepDownT >= RATE_WINDOW_MS
    ) {
      this.fps = Math.max(this.floor(), this.fps * RATE_STEP_DOWN);
      this.lastStepDownT = t;
      this.quietSince = t;
      this.window = [];
      return;
    }
    if (this.fps < this.target && t - this.quietSince >= RATE_RECOVER_MS) {
      this.fps = Math.min(this.target, this.fps * RATE_STEP_UP);
      this.quietSince = t;
    }
  }
}

/**
 * Lets webcam frames through so that the processed rate follows `currentFps()`: frames are due at
 * a fixed interval; a frame that arrives late is processed at once, but a delay is never made up
 * for with a burst.
 */
export class FrameGate {
  private nextDue: number | null = null;

  constructor(private readonly rate: { currentFps(): number }) {}

  shouldProcess(t: number): boolean {
    const interval = 1000 / this.rate.currentFps();
    const due = this.nextDue;
    if (due === null) {
      this.nextDue = t + interval;
      return true;
    }
    if (t < due - GATE_TOLERANCE_SHARE * interval) return false;
    this.nextDue = t - due > interval ? t + interval : due + interval;
    return true;
  }
}
