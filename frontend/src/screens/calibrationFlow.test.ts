import { describe, expect, it } from "vitest";

import protocol from "../../../shared/protocol.json";
import type { StudyConfig } from "../config";
import type { GateReason } from "../eventLog";
import type { CalibrationGate } from "../face/calibrationGate";
import { GATE_MESSAGES, calibrationFlow } from "./calibrationFlow";

const DEFAULT = protocol.study_config_default as unknown as StudyConfig;

function config(
  change: Partial<StudyConfig["calibration_gate"]> = {},
  beforeVideo = true,
): StudyConfig {
  return {
    ...DEFAULT,
    validation: { ...DEFAULT.validation, before_video: beforeVideo },
    calibration_gate: { ...DEFAULT.calibration_gate, ...change },
  };
}

/** A fake gate that answers the given decisions in order. */
function fakeGate(enabled: boolean, decisions: Array<{ passed: boolean; reason: GateReason }>) {
  const calls: string[] = [];
  const gate = {
    enabled,
    beginAttempt: (t: number) => calls.push(`begin@${t}`),
    skipPreVideo: (attempt: number) => calls.push(`skip:${attempt}`),
    evaluatePreVideo: (attempt: number) => {
      calls.push(`evaluate:${attempt}`);
      const next = decisions.shift();
      if (next === undefined) throw new Error("no decision left");
      return next;
    },
  };
  return { gate: gate as unknown as CalibrationGate, calls };
}

function run(cfg: StudyConfig, gate: CalibrationGate) {
  const trace: string[] = [];
  const messages: string[] = [];
  const result = calibrationFlow({
    config: cfg,
    gate,
    now: () => 42,
    calibrate: async () => void trace.push("calibrate"),
    validate: async () => void trace.push("validate"),
    retry: async (message) => {
      trace.push("retry");
      messages.push(message);
    },
    end: async () => void trace.push("end"),
  });
  return { result, trace, messages };
}

const FAIL = (reason: GateReason) => ({ passed: false, reason });
const PASS = { passed: true, reason: "ok" as GateReason };

describe("calibrationFlow", () => {
  it("one calibration and validation when the gate passes", async () => {
    const { gate, calls } = fakeGate(true, [PASS]);
    const { result, trace } = run(config(), gate);
    expect(await result).toBe("continue");
    expect(trace).toEqual(["calibrate", "validate"]);
    expect(calls).toEqual(["begin@42", "evaluate:1"]);
  });

  it("retries with the most likely cause shown, then continues after a pass", async () => {
    const { gate, calls } = fakeGate(true, [FAIL("low_brightness"), PASS]);
    const { result, trace, messages } = run(config(), gate);
    expect(await result).toBe("continue");
    expect(trace).toEqual(["calibrate", "validate", "retry", "calibrate", "validate"]);
    expect(messages).toEqual(["Add light in front of you."]);
    expect(calls).toEqual(["begin@42", "evaluate:1", "begin@42", "evaluate:2"]);
  });

  it("after max_attempts failures it continues flagged (the server derives the flag)", async () => {
    const { gate } = fakeGate(true, [
      FAIL("high_error"),
      FAIL("high_error"),
      FAIL("few_face_frames"),
    ]);
    const { result, trace, messages } = run(config({ max_attempts: 3 }), gate);
    expect(await result).toBe("continue");
    expect(trace.filter((t) => t === "calibrate")).toHaveLength(3);
    expect(messages).toHaveLength(2); // no retry message after the last attempt
    expect(trace).not.toContain("end");
  });

  it("on_fail end_session ends the study after the last failed attempt", async () => {
    const { gate } = fakeGate(true, [FAIL("high_error"), FAIL("high_error")]);
    const { result, trace } = run(config({ max_attempts: 2, on_fail: "end_session" }), gate);
    expect(await result).toBe("end");
    expect(trace.at(-1)).toBe("end");
  });

  it("a study without the pre-video validation skips the gate and logs that", async () => {
    const { gate, calls } = fakeGate(true, []);
    const { result, trace } = run(config({}, false), gate);
    expect(await result).toBe("continue");
    expect(trace).toEqual(["calibrate"]);
    expect(calls).toEqual(["begin@42", "skip:1"]);
  });

  it("a disabled gate makes exactly one attempt and never retries", async () => {
    const { gate } = fakeGate(false, [PASS]);
    const { result, trace } = run(config({ enabled: false, max_attempts: 3 }), gate);
    expect(await result).toBe("continue");
    expect(trace).toEqual(["calibrate", "validate"]);
  });
});

describe("GATE_MESSAGES", () => {
  it("has a non-technical sentence for every reason that can fail a gate", () => {
    for (const reason of [
      "few_face_frames",
      "low_brightness",
      "head_or_distance_change",
      "high_error",
      "forced_debug",
    ] as const) {
      expect(GATE_MESSAGES[reason]).toMatch(/^[A-Z].*\.$/);
      expect(GATE_MESSAGES[reason]).not.toMatch(/\d/);
    }
  });
});
