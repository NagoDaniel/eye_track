// Shared timebase (PLAN §5.3): every client timestamp `t` is performance.now() in ms.
// Date.now() is only used once, to map the clock to wall time.

/** `t`: milliseconds since the page's time origin, as a float. Never use Date.now() for intervals. */
export function now(): number {
  return performance.now();
}

export interface ClockOrigin {
  time_origin: number;
  wall_clock_ms: number;
}

let origin: ClockOrigin | null = null;

/** performance.timeOrigin and Date.now(), captured once on first call (call it at session start). */
export function clockOrigin(): ClockOrigin {
  origin ??= { time_origin: performance.timeOrigin, wall_clock_ms: Date.now() };
  return origin;
}
