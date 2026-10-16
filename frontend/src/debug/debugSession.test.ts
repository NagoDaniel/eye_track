// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import faceFrames from "../../../shared/fixtures/face_frames.json";
import protocol from "../../../shared/protocol.json";
import type { StudyConfig } from "../config";
import { EventLog, type EventRecord, type EventSink } from "../eventLog";
import { type GateReport } from "../face/calibrationGate";
import type { TrackedFrame } from "../face/faceTracker";
import { frameFeatures } from "../face/features";
import { featureVector } from "../face/quickCalib";
import { fitRidge } from "../face/ridge";
import type { Landmark } from "../records";
import {
  type DebugCapture,
  type DebugGate,
  DebugSession,
  GAZE_SMOOTHING_FRAMES,
  OVERLAY_UPDATE_MS,
} from "./debugSession";

const DEFAULT_CONFIG = protocol.study_config_default as unknown as StudyConfig;

function trackedFrame(index: number, t = 0): TrackedFrame {
  const f = faceFrames.frames[index];
  if (f === undefined) throw new Error("no fixture frame");
  const landmarks = f.lm as Landmark[];
  return {
    t,
    result: { faces: 1, landmarks, matrix: f.matrix, blinkL: f.blink_l, blinkR: f.blink_r },
    features: frameFeatures(landmarks, f.matrix, faceFrames.aspect),
    brightness: 0.5,
    inferenceMs: 9,
    aspect: faceFrames.aspect,
    record: {} as TrackedFrame["record"],
  };
}

let root: HTMLElement;
let config: StudyConfig;
let records: EventRecord[];
let sinks: Set<EventSink>;
let log: EventLog;
let debug: DebugSession;
let capture: DebugCapture;

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = '<main id="app"></main>';
  root = document.getElementById("app") as HTMLElement;
  config = structuredClone(DEFAULT_CONFIG);
  records = [];
  sinks = new Set();
  log = new EventLog(
    (r) => {
      records.push(r);
      for (const sink of sinks) sink(r);
    },
    () => 100,
  );
  capture = {
    stream: {} as MediaStream,
    lastFrame: null,
    webcamFps: { rate: () => 29.5 },
    modelFps: { rate: () => 24.5 },
    inferenceMs: { value: 11.2 },
    droppedStimulusFrames: 3,
    listeners: new Set(),
  };
  debug = new DebugSession({
    root,
    config,
    log,
    eventSinks: sinks,
    queueStats: () => ({ pendingChunks: 2, pendingBytes: 2048 }),
    qualityDefaults: DEFAULT_CONFIG.quality,
    createPreview: () => document.createElement("video"),
  });
  debug.attach(capture);
});
afterEach(() => {
  debug.destroy();
  vi.useRealTimers();
});

const press = (key: string, target: EventTarget = document.body): void => {
  target.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
};
const panelText = (): string => root.querySelector(".debug-panel")?.textContent ?? "";
const rootEl = (): HTMLElement => root.querySelector(".debug-root") as HTMLElement;
const gazeDot = (): HTMLElement => root.querySelector(".debug-gaze-dot") as HTMLElement;

/** A real model: mapping of the iris position to the viewport, fitted on the real frames. */
function realModel(): DebugGate["model"] {
  const vectors = faceFrames.frames.map((f) =>
    featureVector(frameFeatures(f.lm as Landmark[], f.matrix, faceFrames.aspect)),
  );
  return fitRidge(
    vectors,
    vectors.map((v) => [800 + 1000 * (v[0] ?? 0), 450 + 1000 * (v[1] ?? 0)] as [number, number]),
  );
}

describe("DebugSession keyboard", () => {
  it("D shows and hides the overlay", () => {
    expect(rootEl().hidden).toBe(false);
    press("d");
    expect(rootEl().hidden).toBe(true);
    press("D");
    expect(rootEl().hidden).toBe(false);
  });

  it("ignores keys typed into a slider or text field", () => {
    const input = document.createElement("input");
    document.body.append(input);
    press("d", input);
    expect(rootEl().hidden).toBe(false);
  });

  it("S logs debug_skip with what is running and fires the skip bus; nothing runs: nothing happens", () => {
    let skipped = 0;
    debug.skipBus.onSkip(() => skipped++);
    press("s");
    expect(records).toEqual([]);
    expect(skipped).toBe(0);
    debug.setTarget("calibration");
    press("s");
    expect(records.map((r) => [r.type, r.data])).toEqual([
      ["debug_skip", { target: "calibration" }],
    ]);
    expect(skipped).toBe(1);
    debug.setTarget("video");
    press("s");
    expect(records.at(-1)?.data).toEqual({ target: "video" });
  });

  it("F toggles the forced gate failure that the gate reads", () => {
    expect(debug.forceFailure()).toBe(false);
    press("f");
    expect(debug.forceFailure()).toBe(true);
    press("f");
    expect(debug.forceFailure()).toBe(false);
  });
});

describe("DebugSession live gaze dot", () => {
  const frame = (i: number): void => capture.listeners.forEach((l) => l(trackedFrame(i)));

  it("stays hidden until the gate has a model", () => {
    frame(0);
    expect(gazeDot().style.display).toBe("none");
  });

  it("follows the quick model's prediction, smoothed over the last frames, and G hides it", () => {
    const gate: DebugGate = { model: realModel(), lastReport: null };
    debug.setGate(gate);
    frame(3);
    expect(gazeDot().style.display).toBe("block");
    expect(gazeDot().style.transform).toMatch(/^translate\(-?[\d.]+px, -?[\d.]+px\)$/);
    for (let i = 0; i < GAZE_SMOOTHING_FRAMES + 2; i++) frame(i);
    expect(gazeDot().style.display).toBe("block");
    press("g");
    expect(gazeDot().style.display).toBe("none");
    frame(5);
    expect(gazeDot().style.display).toBe("none");
  });

  it("hides the dot when the face is lost", () => {
    debug.setGate({ model: realModel(), lastReport: null });
    frame(1);
    capture.listeners.forEach((l) => l({ ...trackedFrame(1), features: null }));
    expect(gazeDot().style.display).toBe("none");
  });
});

describe("DebugSession panel", () => {
  it("shows counters, quality signals, the event tail and the gate numbers", async () => {
    capture.lastFrame = trackedFrame(2);
    debug.setReferenceFaceRatio(0.07);
    const report: GateReport = {
      block: "before_video",
      attempt: 2,
      decision: { passed: false, reason: "high_error" },
      measured: { meanErrorNorm: 0.123, pointErrors: [0.1, null], validFramesPerDot: [9, 8] },
      points: [
        { target: { x: 100, y: 100 }, prediction: { x: 200, y: 150 } },
        { target: { x: 500, y: 300 }, prediction: null },
      ],
    };
    debug.setGate({ model: null, lastReport: report });
    log.log("video_play", { video_id: "1", media_time: 0 });
    await vi.advanceTimersByTimeAsync(OVERLAY_UPDATE_MS);
    const text = panelText();
    expect(text).toContain("29.5"); // webcam fps
    expect(text).toContain("24.5"); // face model fps
    expect(text).toContain("11.2 ms");
    expect(text).toContain("2.0 KB");
    expect(text).toContain("0.50"); // brightness
    expect(text).toContain("video_play");
    expect(text).toContain("attempt 2: FAILED");
    expect(text).toContain("0.123");
  });

  it("a threshold slider changes the config's own quality block, so the gate and the check see it", async () => {
    const input = root.querySelector('input[data-key="brightness_min"]') as HTMLInputElement;
    input.value = "0.4";
    input.dispatchEvent(new Event("input"));
    expect(config.quality.brightness_min).toBe(0.4);
    expect(config.quality.brightness_max).toBe(DEFAULT_CONFIG.quality.brightness_max);
  });

  it("destroy removes the overlay, the gaze dot and the key listener", () => {
    debug.destroy();
    expect(root.querySelector(".debug-root")).toBeNull();
    expect(root.querySelector(".debug-gaze-dot")).toBeNull();
    debug.setTarget("video");
    press("s");
    expect(records).toEqual([]);
    expect(sinks.size).toBe(0);
  });
});
