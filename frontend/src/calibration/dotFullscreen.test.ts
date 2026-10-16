// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CALIB_CLICK_WINDOW_MS, CALIB_POST_CLICK_MS } from "../constants";
import { EventLog, type EventRecord } from "../eventLog";
import { runDot, runDotBlock } from "./dot";

const DURATION = 1500;
const SPEC = { phase: "calibration", id: 2, x: 100, y: 80 } as const;

let stage: HTMLElement;
let records: EventRecord[];
let log: EventLog;
let fullscreenElement: Element | null;
let doc: Document;
let requestFullscreen: ReturnType<typeof vi.fn>;
const clock = (): number => performance.now();

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = '<div id="stage"></div>';
  stage = document.getElementById("stage") as HTMLElement;
  records = [];
  log = new EventLog((r) => records.push(r), clock);
  fullscreenElement = document.documentElement;
  requestFullscreen = vi.fn(() => Promise.resolve());
  const fake = Object.assign(new EventTarget(), {
    documentElement: { requestFullscreen },
  });
  Object.defineProperty(fake, "fullscreenElement", { get: () => fullscreenElement });
  doc = fake as unknown as Document;
});
afterEach(() => vi.useRealTimers());

const types = (): string[] => records.map((r) => r.type);
const hit = (): HTMLElement | null => stage.querySelector(".dot-hit");
const outer = (): HTMLElement => stage.querySelector(".dot-outer") as HTMLElement;
const overlay = (): HTMLElement => stage.querySelector(".player-overlay") as HTMLElement;
const deps = (
  required = true,
): { clock: () => number; fullscreen: { required: boolean; doc: Document } } => ({
  clock,
  fullscreen: { required, doc },
});
function setFullscreen(on: boolean): void {
  fullscreenElement = on ? document.documentElement : null;
  doc.dispatchEvent(new Event("fullscreenchange"));
}

describe("dots and fullscreen (PLAN §5.2)", () => {
  it("removes the dot with a hide event and shows the overlay when fullscreen is left mid-dot", async () => {
    void runDot(stage, SPEC, DURATION, log, deps());
    expect(overlay().hidden).toBe(true);
    await vi.advanceTimersByTimeAsync(500);
    setFullscreen(false);
    expect(hit()).toBeNull();
    expect(overlay().hidden).toBe(false);
    expect(overlay().textContent).toContain("Return to fullscreen to continue");
    expect(overlay().querySelector("button")?.textContent).toBe("Enter fullscreen");
    expect(types()).toEqual(["calib_dot_show", "calib_dot_hide"]);
    expect(records[1]?.data).toEqual({ phase: "calibration", id: 2 });
    await vi.advanceTimersByTimeAsync(5000);
    expect(hit()).toBeNull(); // nothing proceeds while out of fullscreen
  });

  it("restarts the same id with a new show event and the full duration on re-entry", async () => {
    let resolved = false;
    void runDot(stage, SPEC, DURATION, log, deps()).then(() => (resolved = true));
    await vi.advanceTimersByTimeAsync(1000);
    setFullscreen(false);
    await vi.advanceTimersByTimeAsync(3000);
    setFullscreen(true);
    expect(overlay().hidden).toBe(true);
    expect(types()).toEqual(["calib_dot_show", "calib_dot_hide", "calib_dot_show"]);
    expect(records[2]?.data).toEqual({ phase: "calibration", id: 2, x: 100, y: 80 });
    expect(outer().style.width).toBe("40px"); // full size again

    // an early click (first 900 ms of the new run) is still rejected, as for a fresh dot
    await vi.advanceTimersByTimeAsync(DURATION - CALIB_CLICK_WINDOW_MS - 50);
    hit()?.click();
    expect(types()).not.toContain("calib_dot_click");
    // after the full duration the click is accepted
    await vi.advanceTimersByTimeAsync(50 + CALIB_CLICK_WINDOW_MS);
    expect(Number.parseFloat(outer().style.width)).toBeLessThan(13); // shrunk to about 12 px
    hit()?.click();
    await vi.advanceTimersByTimeAsync(CALIB_POST_CLICK_MS);
    expect(types()).toEqual([
      "calib_dot_show",
      "calib_dot_hide",
      "calib_dot_show",
      "calib_dot_click",
      "calib_dot_hide",
    ]);
    expect(resolved).toBe(true);
    expect(overlay()).toBeNull();
  });

  it("does not restart or remove a dot that was already clicked", async () => {
    let resolved = false;
    void runDot(stage, SPEC, DURATION, log, deps()).then(() => (resolved = true));
    await vi.advanceTimersByTimeAsync(DURATION);
    hit()?.click();
    setFullscreen(false);
    expect(hit()).not.toBeNull();
    expect(overlay().hidden).toBe(true);
    setFullscreen(true);
    await vi.advanceTimersByTimeAsync(CALIB_POST_CLICK_MS);
    expect(types()).toEqual(["calib_dot_show", "calib_dot_click", "calib_dot_hide"]);
    expect(resolved).toBe(true);
  });

  it("ignores fullscreen when it is not required", async () => {
    fullscreenElement = null;
    void runDot(stage, SPEC, DURATION, log, deps(false));
    expect(hit()).not.toBeNull();
    setFullscreen(false);
    setFullscreen(true);
    setFullscreen(false);
    expect(hit()).not.toBeNull();
    expect(overlay().hidden).toBe(true);
    expect(types()).toEqual(["calib_dot_show"]);
  });

  it("blocks the first dot until fullscreen is entered when it was never entered", async () => {
    fullscreenElement = null;
    void runDot(stage, SPEC, DURATION, log, deps());
    expect(hit()).toBeNull();
    expect(overlay().hidden).toBe(false);
    expect(types()).toEqual([]); // never shown, so no hide either
    await vi.advanceTimersByTimeAsync(5000);
    expect(types()).toEqual([]);
    setFullscreen(true);
    expect(hit()).not.toBeNull();
    expect(overlay().hidden).toBe(true);
    expect(types()).toEqual(["calib_dot_show"]);
  });

  it("the overlay button requests fullscreen", async () => {
    fullscreenElement = null;
    void runDot(stage, SPEC, DURATION, log, deps());
    (overlay().querySelector("button") as HTMLButtonElement).click();
    expect(requestFullscreen).toHaveBeenCalledTimes(1);
  });

  it("a refused fullscreen request rejects the block instead of crashing", async () => {
    fullscreenElement = null;
    const refusal = new Error("fullscreen refused");
    requestFullscreen.mockReturnValue(Promise.reject(refusal));
    const block = runDotBlock(stage, "validation", [{ x: 1, y: 1 }], DURATION, log, deps());
    const outcome = expect(block).rejects.toBe(refusal);
    (overlay().querySelector("button") as HTMLButtonElement).click();
    await outcome;
    expect(types()).toEqual([]);
  });

  it("gates every dot of a block: the next dot waits for fullscreen after a leave between dots", async () => {
    const points = [
      { x: 1, y: 1 },
      { x: 2, y: 2 },
    ];
    let done = false;
    void runDotBlock(stage, "calibration", points, DURATION, log, deps()).then(() => (done = true));
    await vi.advanceTimersByTimeAsync(DURATION);
    (document.querySelector(".dot-hit") as HTMLElement).click();
    fullscreenElement = null; // left during the final hold; the dot is not touched
    await vi.advanceTimersByTimeAsync(CALIB_POST_CLICK_MS);
    expect(document.querySelector(".dot-hit")).toBeNull();
    expect(document.querySelector(".player-overlay")).not.toBeNull(); // dot 1 is held at its start
    setFullscreen(true);
    await vi.advanceTimersByTimeAsync(DURATION);
    (document.querySelector(".dot-hit") as HTMLElement).click();
    await vi.advanceTimersByTimeAsync(CALIB_POST_CLICK_MS);
    expect(done).toBe(true);
    expect(records.filter((r) => r.type === "calib_dot_show").map((r) => r.data["id"])).toEqual([
      0, 1,
    ]);
  });
});
