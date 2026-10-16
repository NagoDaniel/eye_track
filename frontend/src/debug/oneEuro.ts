// One-euro filter (Casiez et al. 2012) for the live gaze dot (PLAN §12.1): steady while the eyes
// rest, quick to follow when they move. Two coordinates share one speed estimate, so the dot does not
// smooth one axis more than the other. Time-based: frames arrive at uneven intervals.

/** Cut-off frequency at rest (Hz): lower is steadier and laggier. */
export const ONE_EURO_MIN_CUTOFF_HZ = 0.5;
/**
 * How much the cut-off rises with speed (Hz per px/s): lets quick moves through. Small, because the
 * speed of a jittery webcam gaze is mostly noise (tuned on the pilot's moving-dot run: halves the
 * frame-to-frame travel and does not raise the error).
 */
export const ONE_EURO_BETA = 0.0005;
/** Cut-off of the speed estimate (Hz). */
export const ONE_EURO_DERIVATIVE_CUTOFF_HZ = 1.0;
/** After a gap this long without a sample (face lost) the filter starts again from the next one. */
export const ONE_EURO_RESET_GAP_MS = 500;

/** Weight of the newest sample for a low-pass filter with this cut-off, `dtSeconds` apart. */
function smoothingFactor(cutoffHz: number, dtSeconds: number): number {
  const tau = 1 / (2 * Math.PI * cutoffHz);
  return 1 / (1 + tau / dtSeconds);
}

export interface OneEuroOptions {
  minCutoffHz?: number;
  beta?: number;
  derivativeCutoffHz?: number;
  resetGapMs?: number;
}

export class OneEuroFilter2D {
  private readonly minCutoffHz: number;
  private readonly beta: number;
  private readonly derivativeCutoffHz: number;
  private readonly resetGapMs: number;
  private last: { t: number; x: number; y: number; dx: number; dy: number } | null = null;

  constructor(options: OneEuroOptions = {}) {
    this.minCutoffHz = options.minCutoffHz ?? ONE_EURO_MIN_CUTOFF_HZ;
    this.beta = options.beta ?? ONE_EURO_BETA;
    this.derivativeCutoffHz = options.derivativeCutoffHz ?? ONE_EURO_DERIVATIVE_CUTOFF_HZ;
    this.resetGapMs = options.resetGapMs ?? ONE_EURO_RESET_GAP_MS;
  }

  /** The filtered position for the sample `(x, y)` taken at `tMs`. */
  filter(tMs: number, x: number, y: number): [number, number] {
    const last = this.last;
    if (last === null || tMs - last.t > this.resetGapMs || tMs <= last.t) {
      this.last = { t: tMs, x, y, dx: 0, dy: 0 };
      return [x, y];
    }
    const dt = (tMs - last.t) / 1000;
    const alphaD = smoothingFactor(this.derivativeCutoffHz, dt);
    const dx = last.dx + alphaD * ((x - last.x) / dt - last.dx);
    const dy = last.dy + alphaD * ((y - last.y) / dt - last.dy);
    const cutoff = this.minCutoffHz + this.beta * Math.hypot(dx, dy);
    const alpha = smoothingFactor(cutoff, dt);
    const fx = last.x + alpha * (x - last.x);
    const fy = last.y + alpha * (y - last.y);
    this.last = { t: tMs, x: fx, y: fy, dx, dy };
    return [fx, fy];
  }

  reset(): void {
    this.last = null;
  }
}
