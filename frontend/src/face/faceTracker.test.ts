// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

import faceFrames from "../../../shared/fixtures/face_frames.json";
import type { Landmark } from "../records";
import type { ModelOutput } from "./faceModel";
import { FaceTracker, type TrackedFrame } from "./faceTracker";
import { SUBSET_MESH_INDICES, frameFeatures } from "./features";
import type { FaceResult } from "./featureRecord";

/** A fake video: rVFC callbacks are fired by hand. */
function fakeVideo(width = 854, height = 480) {
  let next = 1;
  const callbacks = new Map<number, VideoFrameRequestCallback>();
  const video = {
    videoWidth: width,
    videoHeight: height,
    requestVideoFrameCallback: (cb: VideoFrameRequestCallback) => {
      callbacks.set(next, cb);
      return next++;
    },
    cancelVideoFrameCallback: (handle: number) => void callbacks.delete(handle),
  } as unknown as HTMLVideoElement;
  const fire = (mediaTime: number, presentedFrames: number): void => {
    const [handle, cb] = [...callbacks.entries()][0] ?? [];
    if (handle === undefined || cb === undefined) throw new Error("no callback armed");
    callbacks.delete(handle);
    cb(0, {
      mediaTime,
      presentedFrames,
      captureTime: mediaTime * 1000,
    } as VideoFrameCallbackMetadata);
  };
  return { video, fire, armed: () => callbacks.size };
}

/** The mesh a real fixture frame stands for: the subset at its mesh indices, zeros elsewhere. */
function meshOf(index: number): FaceResult {
  const frame = faceFrames.frames[index];
  if (frame === undefined) throw new Error("no frame");
  const mesh: Landmark[] = Array.from({ length: 478 }, () => [0, 0, 0] as Landmark);
  SUBSET_MESH_INDICES.forEach((meshIndex, position) => {
    mesh[meshIndex] = frame.lm[position] as Landmark;
  });
  return {
    faces: 1,
    landmarks: mesh,
    matrix: frame.matrix,
    blinkL: frame.blink_l,
    blinkR: frame.blink_r,
  };
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};

function setup(results: Array<FaceResult | Promise<FaceResult>>) {
  const { video, fire, armed } = fakeVideo();
  const frames: TrackedFrame[] = [];
  const errors: Error[] = [];
  const webcam: number[] = [];
  let clock = 0;
  const detected: number[] = [];
  const bitmap = {} as ImageBitmap;
  const tracker = new FaceTracker({
    video,
    model: {
      detect: async (_b, ts): Promise<ModelOutput> => {
        detected.push(ts);
        const result = await results.shift();
        if (result === undefined) throw new Error("no result left");
        return { result, inferenceMs: 9 };
      },
    },
    gate: { shouldProcess: () => true },
    onFrame: (f) => frames.push(f),
    onError: (e) => errors.push(e),
    onWebcamFrame: (t) => webcam.push(t),
    clock: () => (clock += 33),
    grabPixels: () => ({ data: new Uint8ClampedArray(4 * 4 * 4).fill(128), width: 4, height: 4 }),
    grabBitmap: async () => bitmap,
  });
  return { tracker, fire, armed, frames, errors, webcam, detected };
}

describe("FaceTracker", () => {
  it("turns a real frame into a feature record, features and a brightness", async () => {
    const { tracker, fire, frames, errors, detected } = setup([meshOf(3)]);
    tracker.start();
    fire(1.5, 45);
    await flush();
    expect(errors).toEqual([]);
    const [frame] = frames;
    if (frame === undefined) throw new Error("no frame");
    const real = faceFrames.frames[3];
    if (real === undefined) throw new Error("no fixture frame");
    expect(detected).toEqual([33]);
    expect(frame.t).toBe(33);
    expect(frame.aspect).toBeCloseTo(854 / 480, 9);
    expect(frame.record).toMatchObject({
      t: 33,
      webcam_media_time: 1.5,
      presented_frames: 45,
      face: true,
    });
    expect(frame.record.lm).toHaveLength(49);
    expect(frame.features?.faceRatio).toBeCloseTo(
      frameFeatures(real.lm as Landmark[], real.matrix, 854 / 480).faceRatio,
      4,
    );
    expect(frame.inferenceMs).toBe(9);
    // the face box covers part of the flat grey (128/255) test image or none of it; either way a number or null
    expect(frame.brightness === null || (frame.brightness > 0.49 && frame.brightness < 0.51)).toBe(
      true,
    );
  });

  it("a frame without a face gives a record without measurements and no features", async () => {
    const { tracker, fire, frames } = setup([
      { faces: 0, landmarks: null, matrix: null, blinkL: 0, blinkR: 0 },
    ]);
    tracker.start();
    fire(0, 1);
    await flush();
    expect(frames[0]?.record).toMatchObject({
      face: false,
      lm: null,
      matrix: null,
      brightness: null,
    });
    expect(frames[0]?.features).toBeNull();
  });

  it("skips frames that arrive while the model is busy, and counts them", async () => {
    let finish: (r: FaceResult) => void = () => undefined;
    const slow = new Promise<FaceResult>((resolve) => (finish = resolve));
    const { tracker, fire, frames, detected } = setup([slow, meshOf(0)]);
    tracker.start();
    fire(0, 1);
    fire(0.03, 2); // busy
    fire(0.06, 3); // busy
    expect(tracker.stats).toMatchObject({ webcamFrames: 3, skippedBusy: 2 });
    finish(meshOf(1));
    await flush();
    fire(0.09, 4);
    await flush();
    expect(detected).toHaveLength(2);
    expect(frames).toHaveLength(2);
    expect(tracker.stats.modelFrames).toBe(2);
  });

  it("respects the rate gate but still counts every webcam frame", async () => {
    const { video, fire } = fakeVideo();
    const processed: number[] = [];
    const webcam: number[] = [];
    let clock = 0;
    const tracker = new FaceTracker({
      video,
      model: { detect: async () => ({ result: meshOf(0), inferenceMs: 1 }) },
      gate: { shouldProcess: (t) => t > 70 },
      onFrame: (f) => processed.push(f.t),
      onError: () => undefined,
      onWebcamFrame: (t) => webcam.push(t),
      clock: () => (clock += 33),
      grabPixels: () => null,
      grabBitmap: async () => ({}) as ImageBitmap,
    });
    tracker.start();
    for (let i = 0; i < 4; i++) {
      fire(i / 30, i);
      await flush();
    }
    expect(webcam).toEqual([33, 66, 99, 132]);
    expect(processed).toEqual([99, 132]);
  });

  it("a failing frame is reported and the loop carries on", async () => {
    const { tracker, fire, frames, errors } = setup([
      Promise.reject(new Error("model crashed")),
      meshOf(2),
    ]);
    tracker.start();
    fire(0, 1);
    await flush();
    fire(0.03, 2);
    await flush();
    expect(errors.map((e) => e.message)).toEqual(["model crashed"]);
    expect(frames).toHaveLength(1);
  });

  it("stop cancels the pending callback and ignores later ones", async () => {
    const { tracker, armed, frames } = setup([meshOf(0)]);
    tracker.start();
    expect(armed()).toBe(1);
    tracker.stop();
    expect(armed()).toBe(0);
    expect(frames).toEqual([]);
  });

  it("throws when requestVideoFrameCallback is missing", () => {
    const video = {} as HTMLVideoElement;
    expect(
      () =>
        new FaceTracker({
          video,
          model: { detect: vi.fn() },
          gate: { shouldProcess: () => true },
          onFrame: () => undefined,
          onError: () => undefined,
        }),
    ).toThrow(/requestVideoFrameCallback/);
  });
});
