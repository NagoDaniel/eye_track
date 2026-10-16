// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";

import faceFrames from "../../../shared/fixtures/face_frames.json";
import protocol from "../../../shared/protocol.json";
import type { StudyConfig } from "../config";
import { frameFeatures } from "../face/features";
import type { FaceResult } from "../face/featureRecord";
import type { TrackedFrame } from "../face/faceTracker";
import { CAMERA_MESSAGES, CameraError } from "../media/camera";
import type { Landmark } from "../records";
import {
  DEVICE_CHECK_INTRO,
  DEVICE_CHECK_OK,
  DEVICE_CHECK_WAITING,
  type CheckSource,
  checkMessage,
  deviceCheckScreen,
  frameIssues,
} from "./deviceCheck";

const LIMITS = (protocol.study_config_default as unknown as StudyConfig).quality;

/** A tracked frame built from a real MediaPipe frame of the fixture. */
function realFrame(index: number, t: number, over: Partial<TrackedFrame> = {}): TrackedFrame {
  const f = faceFrames.frames[index];
  if (f === undefined) throw new Error("no fixture frame");
  const features = frameFeatures(f.lm as Landmark[], f.matrix, faceFrames.aspect);
  const result: FaceResult = {
    faces: 1,
    landmarks: f.lm as Landmark[],
    matrix: f.matrix,
    blinkL: f.blink_l,
    blinkR: f.blink_r,
  };
  return {
    t,
    result,
    features,
    brightness: 0.5,
    inferenceMs: 8,
    aspect: faceFrames.aspect,
    record: {} as TrackedFrame["record"],
    ...over,
  };
}
const NO_FACE = (t: number): TrackedFrame => ({
  ...realFrame(0, t),
  result: { faces: 0, landmarks: null, matrix: null, blinkL: 0, blinkR: 0 },
  features: null,
  brightness: null,
});

let root: HTMLElement;
beforeEach(() => {
  document.body.innerHTML = '<main id="app"></main>';
  root = document.getElementById("app") as HTMLElement;
});

function source(): CheckSource {
  return { stream: {} as MediaStream, listeners: new Set() };
}
const text = (): string => root.textContent ?? "";
const status = (): string => root.querySelectorAll("p")[1]?.textContent ?? "";
const next = (): HTMLButtonElement => root.querySelector("button") as HTMLButtonElement;
const feed = (s: CheckSource, frame: TrackedFrame): void => s.listeners.forEach((l) => l(frame));

describe("frameIssues and checkMessage (real frames)", () => {
  it("finds nothing wrong with the real clip", () => {
    for (let i = 0; i < faceFrames.frames.length; i++) {
      expect(frameIssues(realFrame(i, 0), LIMITS), `frame ${i}`).toEqual([]);
    }
  });

  it("says what to do for each problem", () => {
    expect(frameIssues(NO_FACE(0), LIMITS)).toEqual(["no_face"]);
    expect(checkMessage(frameIssues(NO_FACE(0), LIMITS))).toMatch(/face/i);
    expect(checkMessage(frameIssues(realFrame(0, 0, { brightness: 0.05 }), LIMITS))).toMatch(
      /light/i,
    );
    const two = realFrame(0, 0);
    two.result = { ...two.result, faces: 2 };
    expect(frameIssues(two, LIMITS)).toContain("multiple_faces");
    expect(checkMessage(null)).toBe(DEVICE_CHECK_WAITING);
    expect(checkMessage([])).toBe(DEVICE_CHECK_OK);
  });
});

describe("deviceCheckScreen", () => {
  it("shows the preview and the intro, waits for a face, and enables Continue after 3 s of good frames", async () => {
    const s = source();
    const done = deviceCheckScreen(root, async () => s, LIMITS);
    await Promise.resolve();
    await Promise.resolve();
    expect(text()).toContain(DEVICE_CHECK_INTRO);
    expect(root.querySelector("video.camera-preview")).not.toBeNull();
    expect(status()).toBe(DEVICE_CHECK_WAITING);
    expect(next().disabled).toBe(true);

    feed(s, NO_FACE(0));
    expect(status()).toMatch(/face/i);
    for (let t = 100; t <= 2900; t += 100) feed(s, realFrame(2, t));
    expect(status()).toBe(DEVICE_CHECK_OK);
    expect(next().disabled).toBe(true); // 2.8 s of good frames is not enough
    feed(s, realFrame(3, 3200));
    expect(next().disabled).toBe(false);

    next().click();
    const { capture, referenceFaceRatio } = await done;
    expect(capture).toBe(s);
    expect(referenceFaceRatio).toBeGreaterThan(0.06); // the real clip: about 0.07
    expect(referenceFaceRatio).toBeLessThan(0.08);
    expect(s.listeners.size).toBe(0); // the screen stopped listening
  });

  it("a problem restarts the 3 seconds", async () => {
    const s = source();
    const done = deviceCheckScreen(root, async () => s, LIMITS);
    await Promise.resolve();
    await Promise.resolve();
    for (let t = 0; t <= 2500; t += 100) feed(s, realFrame(2, t));
    feed(s, realFrame(2, 2600, { brightness: 0.05 }));
    for (let t = 2700; t <= 5000; t += 100) feed(s, realFrame(2, t));
    expect(next().disabled).toBe(true);
    feed(s, realFrame(2, 5800));
    expect(next().disabled).toBe(false);
    next().click();
    await done;
  });

  it("shows a camera problem in plain words and tries again on request", async () => {
    const s = source();
    let calls = 0;
    const done = deviceCheckScreen(
      root,
      async () => {
        if (calls++ === 0) throw new CameraError("denied", "NotAllowedError");
        return s;
      },
      LIMITS,
    );
    await Promise.resolve();
    await Promise.resolve();
    expect(text()).toContain(CAMERA_MESSAGES.denied);
    next().click(); // "Try again"
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    expect(text()).toContain(DEVICE_CHECK_INTRO);
    expect(calls).toBe(2);
    for (let t = 0; t <= 3500; t += 100) feed(s, realFrame(1, t));
    next().click();
    await done;
  });

  it("an unexpected failure is not swallowed", async () => {
    await expect(
      deviceCheckScreen(root, () => Promise.reject(new Error("worker failed")), LIMITS),
    ).rejects.toThrow("worker failed");
  });
});
