// One calibration / validation dot (PLAN §5.5): outer radius 20 px shrinking to 6 px over
// `dot_duration_ms`, dark centre 4 px; a click counts only in the last 600 ms.

import { now } from "../clock";
import {
  CALIB_CLICK_WINDOW_MS,
  CALIB_POST_CLICK_MS,
  DOT_CENTRE_RADIUS_PX,
  DOT_EARLY_CLICK_FLASH_MS,
  DOT_OUTER_RADIUS_PX,
  DOT_SHRUNK_RADIUS_PX,
} from "../constants";
import type { DotPhase, EventLog } from "../eventLog";
import { fullscreenGate } from "../playbackGate";
import { button, el, p } from "../ui/dom";
import type { Point } from "./points";

/** Outer radius `elapsedMs` after the dot appeared: linear from 20 px down to 6 px. */
export function dotRadius(elapsedMs: number, durationMs: number): number {
  const progress = Math.min(Math.max(elapsedMs / durationMs, 0), 1);
  return DOT_OUTER_RADIUS_PX + (DOT_SHRUNK_RADIUS_PX - DOT_OUTER_RADIUS_PX) * progress;
}

/**
 * "accept" once the dot is in the last `windowMs` of its shrink time (and stays acceptable afterwards, so a
 * participant who is slow is not stuck); "wait" before that.
 */
export function clickVerdict(
  elapsedMs: number,
  durationMs: number,
  windowMs: number = CALIB_CLICK_WINDOW_MS,
): "accept" | "wait" {
  return elapsedMs >= durationMs - windowMs ? "accept" : "wait";
}

export interface DotSpec extends Point {
  phase: DotPhase;
  id: number;
}

export interface DotDeps {
  clock?: () => number;
  /**
   * PLAN §5.2: with `required` the dots are only shown while the document is in fullscreen. Leaving it removes
   * the current dot (`calib_dot_hide`) and shows a "Return to fullscreen to continue" overlay; on return the SAME
   * dot restarts from its full size (`calib_dot_show` again with the same id). A dot that was already clicked is
   * never restarted. Consumers of the events must therefore use the LAST `calib_dot_click` of each (phase, id)
   * (at most one is ever logged per id, because an accepted click ends the dot's restarts).
   */
  fullscreen?: { required: boolean; doc?: Document };
}

/**
 * Show one dot on `stage`; resolves once the accepted click has been handled and the dot is hidden again.
 * Rejects if the browser refuses the "Enter fullscreen" request of the overlay.
 */
export function runDot(
  stage: HTMLElement,
  spec: DotSpec,
  durationMs: number,
  log: EventLog,
  deps: DotDeps = {},
): Promise<void> {
  const clock = deps.clock ?? now;
  const requireFullscreen = deps.fullscreen?.required ?? false;
  const doc = deps.fullscreen?.doc ?? document;
  return new Promise((resolve, reject) => {
    let hit: HTMLElement | null = null; // the dot currently on the stage
    let start = 0;
    let clicked = false;
    let held = false; // waiting for fullscreen, no dot on the stage
    let frame = 0;

    const onFullscreenChange = (): void => applyGate();
    const cleanup = (): void => {
      doc.removeEventListener("fullscreenchange", onFullscreenChange);
      cancelAnimationFrame(frame);
      overlay.remove();
    };

    const overlay = el(
      "div",
      { class: "player-overlay" },
      p("Return to fullscreen to continue"),
      button("Enter fullscreen", () => {
        doc.documentElement.requestFullscreen().catch((e: unknown) => {
          cleanup();
          reject(e);
        });
      }),
    );
    overlay.hidden = true;

    const showDot = (): void => {
      const outer = el("div", { class: "dot-outer" });
      const centre = el("div", { class: "dot-centre" });
      centre.style.width = centre.style.height = `${2 * DOT_CENTRE_RADIUS_PX}px`;
      const dot = el("div", { class: "dot-hit" }, outer, centre);
      const hitSize = 2 * DOT_OUTER_RADIUS_PX;
      dot.style.width = dot.style.height = `${hitSize}px`;
      dot.style.left = `${spec.x - DOT_OUTER_RADIUS_PX}px`;
      dot.style.top = `${spec.y - DOT_OUTER_RADIUS_PX}px`;

      start = clock();
      const draw = (): void => {
        const size = 2 * dotRadius(clock() - start, durationMs);
        outer.style.width = outer.style.height = `${size}px`;
        if (!clicked && clock() - start < durationMs) frame = requestAnimationFrame(draw);
      };

      dot.addEventListener("click", () => {
        if (clicked || hit !== dot) return;
        if (clickVerdict(clock() - start, durationMs) === "wait") {
          outer.classList.add("flash");
          setTimeout(() => outer.classList.remove("flash"), DOT_EARLY_CLICK_FLASH_MS);
          return;
        }
        clicked = true;
        cancelAnimationFrame(frame);
        log.log("calib_dot_click", { phase: spec.phase, id: spec.id });
        // the dot stays for the rest of the training window (100 ms after the click), also if fullscreen is left
        setTimeout(() => {
          dot.remove();
          hit = null;
          log.log("calib_dot_hide", { phase: spec.phase, id: spec.id });
          cleanup();
          resolve();
        }, CALIB_POST_CLICK_MS);
      });

      hit = dot;
      stage.append(dot);
      draw();
      log.log("calib_dot_show", { phase: spec.phase, id: spec.id, x: spec.x, y: spec.y });
    };

    const removeDot = (): void => {
      cancelAnimationFrame(frame);
      hit?.remove();
      hit = null;
      log.log("calib_dot_hide", { phase: spec.phase, id: spec.id });
    };

    /** Same decision as during playback (`fullscreenGate`): hold when fullscreen is missing, restart when back. */
    const applyGate = (): void => {
      if (clicked) return; // the click was accepted: only the final hold remains
      const result = fullscreenGate({
        requireFullscreen,
        isFullscreen: doc.fullscreenElement !== null,
        pausedForFullscreen: held,
      });
      held = result.pausedForFullscreen;
      if (result.action === "pause" && hit !== null) removeDot();
      if (result.action === "resume") showDot();
      overlay.hidden = !held;
    };

    stage.append(overlay);
    if (requireFullscreen) doc.addEventListener("fullscreenchange", onFullscreenChange);
    applyGate();
    if (!held) showDot();
  });
}

/** Show `points` one after another on a mid-grey stage. Dot ids are the position in the shown order. */
export async function runDotBlock(
  root: HTMLElement,
  phase: DotPhase,
  points: readonly Point[],
  durationMs: number,
  log: EventLog,
  deps: DotDeps = {},
): Promise<void> {
  const stage = el("div", { class: "stage" });
  root.replaceChildren(stage);
  for (const [id, point] of points.entries()) {
    await runDot(stage, { phase, id, ...point }, durationMs, log, deps);
  }
}
