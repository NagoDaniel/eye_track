// Live counters for the debug overlay (PLAN §12.1): rates over a sliding window and a smoothed
// per-frame time. Pure; the clock is injected (use `now` from ../clock in the app).

export const RATE_WINDOW_MS = 2000;
/** Weight of the newest sample in the inference-time average. */
export const EMA_ALPHA = 0.1;

export type Clock = () => number;

/** Events per second over the last `windowMs` (webcam fps, face-model fps). */
export class RateCounter {
  private stamps: number[] = [];
  private readonly start: number;

  constructor(
    private readonly clock: Clock,
    private readonly windowMs: number = RATE_WINDOW_MS,
  ) {
    this.start = clock();
  }

  tick(): void {
    this.stamps.push(this.clock());
  }

  /** Events per second. Before one full window has passed, the elapsed time is used instead. */
  rate(): number {
    const t = this.clock();
    const from = t - this.windowMs;
    let first = 0;
    while (first < this.stamps.length && (this.stamps[first] ?? 0) <= from) first += 1;
    if (first > 0) this.stamps = this.stamps.slice(first);
    const span = Math.min(this.windowMs, t - this.start);
    return span > 0 ? (this.stamps.length * 1000) / span : 0;
  }
}

/** Exponential moving average. `value` is null until the first sample. */
export class Ema {
  private current: number | null = null;

  constructor(private readonly alpha: number = EMA_ALPHA) {}

  add(sample: number): void {
    this.current =
      this.current === null ? sample : this.current + this.alpha * (sample - this.current);
  }

  get value(): number | null {
    return this.current;
  }
}
