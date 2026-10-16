// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SkipBus } from "../debug/skip";
import { EventLog, type EventRecord } from "../eventLog";
import { runDotBlock } from "./dot";

const clock = (): number => performance.now();
let root: HTMLElement;
let records: EventRecord[];
let log: EventLog;

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = '<main id="app"></main>';
  root = document.getElementById("app") as HTMLElement;
  records = [];
  log = new EventLog((r) => records.push(r), clock);
});
afterEach(() => vi.useRealTimers());

const POINTS = [
  { x: 10, y: 10 },
  { x: 20, y: 20 },
  { x: 30, y: 30 },
];

describe("debug skip of a dot block (PLAN §12.1)", () => {
  it("ends the dot on the stage and the rest of the block", async () => {
    const skip = new SkipBus();
    let done = false;
    void runDotBlock(root, "calibration", POINTS, 1500, log, { clock, skip }).then(
      () => (done = true),
    );
    await vi.advanceTimersByTimeAsync(300);
    expect(records.map((r) => r.type)).toEqual(["calib_dot_show"]);
    skip.fire();
    await vi.advanceTimersByTimeAsync(0);
    expect(done).toBe(true);
    expect(root.querySelector(".dot-hit")).toBeNull();
    // the dot that was cut short is closed with a hide, and no further dot was shown
    expect(records.map((r) => r.type)).toEqual(["calib_dot_show", "calib_dot_hide"]);
    await vi.advanceTimersByTimeAsync(5000);
    expect(records).toHaveLength(2);
  });

  it("stops listening when the block is over, so a later skip does nothing", async () => {
    const skip = new SkipBus();
    const block = runDotBlock(root, "validation", [POINTS[0] ?? { x: 0, y: 0 }], 1500, log, {
      clock,
      skip,
    });
    await vi.advanceTimersByTimeAsync(10);
    skip.fire();
    await block;
    expect(skip.fire()).toBe(0);
  });

  it("without a skip signal nothing changes", async () => {
    let done = false;
    void runDotBlock(root, "calibration", [POINTS[0] ?? { x: 0, y: 0 }], 1500, log, { clock }).then(
      () => (done = true),
    );
    await vi.advanceTimersByTimeAsync(2000);
    expect(done).toBe(false); // still waiting for the click
  });
});
