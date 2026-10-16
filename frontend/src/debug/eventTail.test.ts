import { describe, expect, it } from "vitest";

import type { EventRecord } from "../eventLog";
import { EVENT_DATA_MAX_CHARS, EVENT_TAIL_SIZE, EventTail, formatEventLine } from "./eventTail";

const rec = (t: number, data: Record<string, unknown> = {}): EventRecord => ({
  t,
  type: "calib_dot_show",
  data,
});

describe("formatEventLine", () => {
  it("shows t in seconds with 2 decimals, the type and compact data", () => {
    expect(formatEventLine(rec(12345.678, { phase: "calibration", id: 3 }))).toBe(
      '12.35 calib_dot_show {"phase":"calibration","id":3}',
    );
  });

  it("leaves out empty data", () => {
    expect(formatEventLine({ t: 500, type: "window_blur", data: {} })).toBe("0.50 window_blur");
  });

  it("cuts long data to the named length and marks the cut", () => {
    const line = formatEventLine(rec(0, { text: "x".repeat(500) }));
    const data = line.slice("0.00 calib_dot_show ".length);
    expect(data).toHaveLength(EVENT_DATA_MAX_CHARS);
    expect(data.endsWith("…")).toBe(true);
  });

  it("keeps data of exactly the maximum length", () => {
    const pad = "x".repeat(EVENT_DATA_MAX_CHARS - '{"a":""}'.length);
    const line = formatEventLine(rec(0, { a: pad }));
    expect(line.endsWith("…")).toBe(false);
  });
});

describe("EventTail", () => {
  it("keeps only the last EVENT_TAIL_SIZE events, oldest first", () => {
    const tail = new EventTail();
    for (let i = 0; i < EVENT_TAIL_SIZE + 5; i++) tail.push(rec(i * 1000, { id: i }));
    const lines = tail.lines();
    expect(lines).toHaveLength(EVENT_TAIL_SIZE);
    expect(lines[0]).toBe('5.00 calib_dot_show {"id":5}');
    expect(lines[lines.length - 1]).toBe(
      `${EVENT_TAIL_SIZE + 4}.00 calib_dot_show {"id":${EVENT_TAIL_SIZE + 4}}`,
    );
  });

  it("holds 20 events by default", () => {
    expect(EVENT_TAIL_SIZE).toBe(20);
  });

  it("is empty at the start and below capacity keeps everything", () => {
    const tail = new EventTail(3);
    expect(tail.lines()).toEqual([]);
    tail.push(rec(1000));
    tail.push(rec(2000));
    expect(tail.lines()).toEqual(["1.00 calib_dot_show", "2.00 calib_dot_show"]);
  });
});
