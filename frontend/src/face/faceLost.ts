// Debounced face_lost / face_regained events and the "no face" banner (PLAN §5.2 end, §5.7).
// Pure; nothing here pauses anything, the dropout itself is data.

/** A loss is reported only after the face was absent this long, and ended after it was back this long. */
export const FACE_LOST_DEBOUNCE_MS = 500;
/** The non-blocking banner shows when there has been no face for more than this long. */
export const FACE_LOST_BANNER_MS = 3000;

/**
 * `t` is the time of the update that confirmed the event, so events stay in time order with the
 * rest of the event log. The exact loss duration is `lost_ms`; the raw frames carry the exact
 * times of the dropout.
 */
export type FaceLostEvent =
  | { t: number; type: "face_lost"; data: Record<string, never> }
  | { t: number; type: "face_regained"; data: { lost_ms: number } };

export class FaceLostTracker {
  /** Confirmed state: the loss started at this time (first frame without a face), else null. */
  private lostSince: number | null = null;
  /** Start of the current run of frames without a face while not (yet) lost. */
  private absentSince: number | null = null;
  /** Start of the current run of frames with a face while lost. */
  private presentSince: number | null = null;

  /** One call per processed frame. Returns the events this frame confirms (at most one). */
  update(t: number, hasFace: boolean): FaceLostEvent[] {
    if (this.lostSince === null) {
      if (hasFace) {
        this.absentSince = null;
        return [];
      }
      this.absentSince ??= t;
      if (t - this.absentSince < FACE_LOST_DEBOUNCE_MS) return [];
      this.lostSince = this.absentSince;
      this.absentSince = null;
      return [{ t, type: "face_lost", data: {} }];
    }
    if (!hasFace) {
      this.presentSince = null;
      return [];
    }
    this.presentSince ??= t;
    if (t - this.presentSince < FACE_LOST_DEBOUNCE_MS) return [];
    const lostMs = this.presentSince - this.lostSince;
    this.lostSince = null;
    this.presentSince = null;
    return [{ t, type: "face_regained", data: { lost_ms: lostMs } }];
  }

  /** True while a confirmed loss has lasted more than FACE_LOST_BANNER_MS. */
  bannerVisible(t: number): boolean {
    return this.lostSince !== null && t - this.lostSince > FACE_LOST_BANNER_MS;
  }
}
