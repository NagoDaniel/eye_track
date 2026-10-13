// Session-wide window events (PLAN §5.5, §5.7): viewport, fullscreen, visibility, focus.

import { contentRect } from "./calibration/points";
import type { EventLog, Size } from "./eventLog";

export class SessionEvents {
  private intrinsic: Size | null = null;
  private started = false;
  private remove: (() => void) | null = null;

  constructor(
    private readonly log: EventLog,
    private readonly win: Window = window,
    private readonly doc: Document = document,
  ) {}

  /** The stimulus video's intrinsic size while one is shown (null otherwise); a change is logged as a viewport_change. */
  setVideoIntrinsic(size: Size | null): void {
    this.intrinsic = size;
    if (this.started) this.logViewport();
  }

  /** Log the current viewport and listen for changes. */
  start(): void {
    if (this.started) return;
    this.started = true;
    this.logViewport();
    const onResize = (): void => this.logViewport();
    const onFullscreen = (): void => {
      this.log.log("fullscreen_change", { fullscreen: this.doc.fullscreenElement !== null });
    };
    const onVisibility = (): void => {
      this.log.log("visibility_change", { state: this.doc.visibilityState });
    };
    const onBlur = (): void => {
      this.log.log("window_blur", {});
    };
    const onFocus = (): void => {
      this.log.log("window_focus", {});
    };
    this.win.addEventListener("resize", onResize);
    this.win.addEventListener("blur", onBlur);
    this.win.addEventListener("focus", onFocus);
    this.doc.addEventListener("fullscreenchange", onFullscreen);
    this.doc.addEventListener("visibilitychange", onVisibility);
    this.remove = () => {
      this.win.removeEventListener("resize", onResize);
      this.win.removeEventListener("blur", onBlur);
      this.win.removeEventListener("focus", onFocus);
      this.doc.removeEventListener("fullscreenchange", onFullscreen);
      this.doc.removeEventListener("visibilitychange", onVisibility);
    };
  }

  stop(): void {
    this.remove?.();
    this.remove = null;
    this.started = false;
  }

  private logViewport(): void {
    const viewport = { width: this.win.innerWidth, height: this.win.innerHeight };
    this.log.log("viewport_change", {
      viewport,
      screen: { width: this.win.screen.width, height: this.win.screen.height },
      device_pixel_ratio: this.win.devicePixelRatio,
      video_rect: this.intrinsic === null ? null : contentRect(viewport, this.intrinsic.width, this.intrinsic.height),
    });
  }
}
