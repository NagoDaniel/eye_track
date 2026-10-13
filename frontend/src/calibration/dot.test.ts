// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CALIB_CLICK_WINDOW_MS,
  CALIB_POST_CLICK_MS,
  DOT_CENTRE_RADIUS_PX,
  DOT_EARLY_CLICK_FLASH_MS,
  DOT_OUTER_RADIUS_PX,
  DOT_SHRUNK_RADIUS_PX,
} from "../constants";
import { EventLog, type EventRecord } from "../eventLog";
import { clickVerdict, dotRadius, runDot, runDotBlock } from "./dot";

const DURATION = 1500;

describe("dotRadius and clickVerdict (pure)", () => {
  it("uses the plan's constants", () => {
    expect(CALIB_CLICK_WINDOW_MS).toBe(600);
    expect(DOT_OUTER_RADIUS_PX).toBe(20);
    expect(DOT_CENTRE_RADIUS_PX).toBe(4);
    expect(DOT_SHRUNK_RADIUS_PX).toBe(6);
  });

  it("shrinks linearly from 20 px to 6 px and stays there", () => {
    expect(dotRadius(0, DURATION)).toBe(20);
    expect(dotRadius(DURATION / 2, DURATION)).toBe(13);
    expect(dotRadius(DURATION, DURATION)).toBe(6);
    expect(dotRadius(DURATION * 3, DURATION)).toBe(6);
    expect(dotRadius(-5, DURATION)).toBe(20);
  });

  it("accepts a click only in the last 600 ms", () => {
    expect(clickVerdict(0, DURATION)).toBe("wait");
    expect(clickVerdict(DURATION - CALIB_CLICK_WINDOW_MS - 1, DURATION)).toBe("wait");
    expect(clickVerdict(DURATION - CALIB_CLICK_WINDOW_MS, DURATION)).toBe("accept");
    expect(clickVerdict(DURATION, DURATION)).toBe("accept");
    expect(clickVerdict(DURATION * 2, DURATION)).toBe("accept");
  });
});

describe("runDot with fake timers", () => {
  let stage: HTMLElement;
  let records: EventRecord[];
  let log: EventLog;
  const clock = (): number => performance.now();

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = '<div id="stage"></div>';
    stage = document.getElementById("stage") as HTMLElement;
    records = [];
    log = new EventLog((r) => records.push(r), clock);
  });
  afterEach(() => vi.useRealTimers());

  const types = (): string[] => records.map((r) => r.type);
  const outer = (): HTMLElement => stage.querySelector(".dot-outer") as HTMLElement;
  const hit = (): HTMLElement => stage.querySelector(".dot-hit") as HTMLElement;

  it("shows the dot at its position and shrinks it over dot_duration_ms", async () => {
    void runDot(stage, { phase: "calibration", id: 3, x: 300, y: 200 }, DURATION, log, { clock });
    expect(records[0]).toMatchObject({ type: "calib_dot_show", data: { phase: "calibration", id: 3, x: 300, y: 200 } });
    expect(hit().style.left).toBe(`${300 - DOT_OUTER_RADIUS_PX}px`);
    expect(hit().style.top).toBe(`${200 - DOT_OUTER_RADIUS_PX}px`);
    expect(outer().style.width).toBe("40px"); // outer radius 20
    await vi.advanceTimersByTimeAsync(DURATION / 2);
    expect(Number.parseFloat(outer().style.width)).toBeCloseTo(26, 0); // radius 13
    await vi.advanceTimersByTimeAsync(DURATION);
    expect(outer().style.width).toBe("12px"); // radius 6
  });

  it("flashes on an early click: no click event, dot stays", async () => {
    let resolved = false;
    void runDot(stage, { phase: "calibration", id: 0, x: 10, y: 10 }, DURATION, log, { clock }).then(() => (resolved = true));
    await vi.advanceTimersByTimeAsync(DURATION - CALIB_CLICK_WINDOW_MS - 50);
    hit().click();
    expect(outer().classList.contains("flash")).toBe(true);
    expect(types()).toEqual(["calib_dot_show"]);
    await vi.advanceTimersByTimeAsync(DOT_EARLY_CLICK_FLASH_MS);
    expect(outer().classList.contains("flash")).toBe(false);
    await vi.advanceTimersByTimeAsync(CALIB_POST_CLICK_MS * 5);
    expect(resolved).toBe(false);
    expect(stage.querySelector(".dot-hit")).not.toBeNull();
  });

  it("accepts a click in the last 600 ms, logs click then hide 100 ms later, and resolves", async () => {
    let resolved = false;
    void runDot(stage, { phase: "validation", id: 1, x: 50, y: 60 }, DURATION, log, { clock }).then(() => (resolved = true));
    await vi.advanceTimersByTimeAsync(DURATION - CALIB_CLICK_WINDOW_MS);
    hit().click();
    expect(types()).toEqual(["calib_dot_show", "calib_dot_click"]);
    expect(records[1]?.data).toEqual({ phase: "validation", id: 1 });
    await vi.advanceTimersByTimeAsync(CALIB_POST_CLICK_MS - 1);
    expect(types()).toEqual(["calib_dot_show", "calib_dot_click"]);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(types()).toEqual(["calib_dot_show", "calib_dot_click", "calib_dot_hide"]);
    expect(records[2]?.data).toEqual({ phase: "validation", id: 1 });
    expect(resolved).toBe(true);
    expect(stage.querySelector(".dot-hit")).toBeNull();
    // t of the click is 900 ms after the show; hide is 100 ms after the click
    expect((records[1]?.t ?? 0) - (records[0]?.t ?? 0)).toBeCloseTo(DURATION - CALIB_CLICK_WINDOW_MS, 0);
    expect((records[2]?.t ?? 0) - (records[1]?.t ?? 0)).toBeCloseTo(CALIB_POST_CLICK_MS, 0);
  });

  it("logs a second click on the same dot only once", async () => {
    void runDot(stage, { phase: "calibration", id: 0, x: 1, y: 1 }, DURATION, log, { clock });
    await vi.advanceTimersByTimeAsync(DURATION);
    hit().click();
    hit().click();
    expect(types().filter((t) => t === "calib_dot_click")).toHaveLength(1);
  });

  it("runDotBlock shows the dots one after another with ids in shown order", async () => {
    const points = [{ x: 1, y: 1 }, { x: 2, y: 2 }];
    let done = false;
    void runDotBlock(stage, "calibration", points, DURATION, log, { clock }).then(() => (done = true));
    for (let i = 0; i < points.length; i++) {
      await vi.advanceTimersByTimeAsync(DURATION);
      (document.querySelector(".dot-hit") as HTMLElement).click();
      await vi.advanceTimersByTimeAsync(CALIB_POST_CLICK_MS);
    }
    expect(done).toBe(true);
    expect(records.filter((r) => r.type === "calib_dot_show").map((r) => r.data["id"])).toEqual([0, 1]);
  });
});
