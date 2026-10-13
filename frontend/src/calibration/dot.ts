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
import { el } from "../ui/dom";
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
export function clickVerdict(elapsedMs: number, durationMs: number, windowMs: number = CALIB_CLICK_WINDOW_MS): "accept" | "wait" {
  return elapsedMs >= durationMs - windowMs ? "accept" : "wait";
}

export interface DotSpec extends Point {
  phase: DotPhase;
  id: number;
}

export interface DotDeps {
  clock?: () => number;
}

/** Show one dot on `stage`; resolves once the accepted click has been handled and the dot is hidden again. */
export function runDot(
  stage: HTMLElement,
  spec: DotSpec,
  durationMs: number,
  log: EventLog,
  deps: DotDeps = {},
): Promise<void> {
  const clock = deps.clock ?? now;
  return new Promise((resolve) => {
    const outer = el("div", { class: "dot-outer" });
    const centre = el("div", { class: "dot-centre" });
    centre.style.width = centre.style.height = `${2 * DOT_CENTRE_RADIUS_PX}px`;
    const hit = el("div", { class: "dot-hit" }, outer, centre);
    const hitSize = 2 * DOT_OUTER_RADIUS_PX;
    hit.style.width = hit.style.height = `${hitSize}px`;
    hit.style.left = `${spec.x - DOT_OUTER_RADIUS_PX}px`;
    hit.style.top = `${spec.y - DOT_OUTER_RADIUS_PX}px`;

    const start = clock();
    let clicked = false;
    let frame = 0;
    const draw = (): void => {
      const size = 2 * dotRadius(clock() - start, durationMs);
      outer.style.width = outer.style.height = `${size}px`;
      if (!clicked && clock() - start < durationMs) frame = requestAnimationFrame(draw);
    };

    hit.addEventListener("click", () => {
      if (clicked) return;
      if (clickVerdict(clock() - start, durationMs) === "wait") {
        outer.classList.add("flash");
        setTimeout(() => outer.classList.remove("flash"), DOT_EARLY_CLICK_FLASH_MS);
        return;
      }
      clicked = true;
      cancelAnimationFrame(frame);
      log.log("calib_dot_click", { phase: spec.phase, id: spec.id });
      // the dot stays for the rest of the training window (100 ms after the click)
      setTimeout(() => {
        hit.remove();
        log.log("calib_dot_hide", { phase: spec.phase, id: spec.id });
        resolve();
      }, CALIB_POST_CLICK_MS);
    });

    stage.append(hit);
    draw();
    log.log("calib_dot_show", { phase: spec.phase, id: spec.id, x: spec.x, y: spec.y });
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
