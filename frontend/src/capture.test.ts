// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import faceFrames from "../../shared/fixtures/face_frames.json";
import protocol from "../../shared/protocol.json";
import { Capture, type CaptureParts, type CaptureSinks } from "./capture";
import type { StudyConfig } from "./config";
import { EventLog, type EventRecord } from "./eventLog";
import type { FaceResult } from "./face/featureRecord";
import { SUBSET_MESH_INDICES } from "./face/features";
import type { CameraHandle } from "./media/camera";
import type { RecorderOptions } from "./media/recorder";
import type { FeatureRecord, Landmark } from "./records";

const CONFIG = protocol.study_config_default as unknown as StudyConfig;

function realFaceResult(index: number): FaceResult {
  const frame = faceFrames.frames[index];
  if (frame === undefined) throw new Error("no frame");
  const mesh: Landmark[] = Array.from({ length: 478 }, () => [0, 0, 0] as Landmark);
  SUBSET_MESH_INDICES.forEach((meshIndex, position) => {
    mesh[meshIndex] = frame.lm[position] as Landmark;
  });
  return { faces: 1, landmarks: mesh, matrix: frame.matrix, blinkL: 0.1, blinkR: 0.1 };
}
const NO_FACE: FaceResult = { faces: 0, landmarks: null, matrix: null, blinkL: 0, blinkR: 0 };

let clock = 0;
let events: EventRecord[];
let features: FeatureRecord[];
let fatals: Error[];
let recorderOptions: RecorderOptions[];
let recorderStops: number;
let cameraStopped: number;
let endedCallbacks: Array<() => void>;
let modelClosed: number;
let nextResult: FaceResult;
let fire: (mediaTime?: number) => void;

function makeParts(over: Partial<CaptureParts> = {}): CaptureParts {
  const callbacks: VideoFrameRequestCallback[] = [];
  fire = (mediaTime = 0) => {
    const cb = callbacks.shift();
    if (cb === undefined) throw new Error("no frame callback armed");
    cb(0, { mediaTime, presentedFrames: 1 } as VideoFrameCallbackMetadata);
  };
  const camera: CameraHandle = {
    stream: {} as MediaStream,
    track: {} as MediaStreamTrack,
    settings: { width: 854, height: 480, frameRate: 30 },
    stop: () => void cameraStopped++,
    onEnded: (cb) => {
      endedCallbacks.push(cb);
      return () => undefined;
    },
  };
  return {
    requestCamera: async () => camera,
    startModel: async () => ({
      detect: async () => ({ result: nextResult, inferenceMs: 8 }),
      close: () => void modelClosed++,
    }),
    startRecorder: (options) => {
      recorderOptions.push(options);
      return {
        mimeType: "video/webm",
        stop: async () => {
          recorderStops++;
          return { chunks: 0, bytes: 0 };
        },
      };
    },
    createVideo: () => {
      const video = document.createElement("video");
      Object.defineProperty(video, "videoWidth", { value: 854 });
      Object.defineProperty(video, "videoHeight", { value: 480 });
      video.play = () => Promise.resolve();
      video.requestVideoFrameCallback = (cb) => callbacks.push(cb);
      video.cancelVideoFrameCallback = () => undefined;
      return video;
    },
    frameIo: { grabPixels: () => null, grabBitmap: async () => ({}) as ImageBitmap },
    ...over,
  };
}

function sinks(): CaptureSinks {
  return {
    log: new EventLog(
      (r) => events.push(r),
      () => clock,
    ),
    pushFeature: (r) => features.push(r),
    enqueueWebcam: async () => 0,
    onFatal: (e) => fatals.push(e),
  };
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};

/** One webcam frame at `t` ms. */
async function frameAt(t: number): Promise<void> {
  clock = t;
  fire();
  await flush();
}

beforeEach(() => {
  clock = 1000;
  events = [];
  features = [];
  fatals = [];
  recorderOptions = [];
  recorderStops = 0;
  cameraStopped = 0;
  endedCallbacks = [];
  modelClosed = 0;
  nextResult = realFaceResult(0);
  vi.spyOn(performance, "now").mockImplementation(() => clock);
});
afterEach(() => vi.restoreAllMocks());

describe("Capture", () => {
  it("tracks from the start but records features only after startRecording", async () => {
    const capture = await Capture.open(CONFIG, sinks(), makeParts());
    const seen: number[] = [];
    capture.listeners.add((f) => seen.push(f.t));
    await frameAt(1100);
    expect(seen).toEqual([1100]);
    expect(features).toEqual([]); // the device check is not recorded
    capture.startRecording();
    await frameAt(1200);
    expect(features.map((r) => r.t)).toEqual([1200]);
    expect(features[0]?.face).toBe(true);
    expect(capture.lastFrame?.features).not.toBeNull();
    await capture.close();
  });

  it("starts the recorder once, with the camera settings and the chunk length of the study", async () => {
    const capture = await Capture.open(CONFIG, sinks(), makeParts());
    capture.startRecording();
    capture.startRecording();
    expect(recorderOptions).toHaveLength(1);
    expect(recorderOptions[0]).toMatchObject({
      settings: { width: 854, height: 480, frameRate: 30 },
      timesliceMs: CONFIG.recording.chunk_ms,
    });
    await capture.stopRecording();
    expect(recorderStops).toBe(1);
    await capture.close();
  });

  it("logs face_lost after 500 ms without a face and face_regained with the duration, only while recording", async () => {
    const capture = await Capture.open(CONFIG, sinks(), makeParts());
    nextResult = NO_FACE;
    await frameAt(1100);
    await frameAt(1700); // not recording yet: nothing is logged
    expect(events).toEqual([]);
    capture.startRecording();
    nextResult = realFaceResult(1);
    await frameAt(2000);
    nextResult = NO_FACE;
    await frameAt(2100);
    await frameAt(2700); // 600 ms without a face
    expect(events.map((e) => e.type)).toEqual(["face_lost"]);
    expect(capture.bannerVisible()).toBe(false);
    await frameAt(5200); // more than 3 s
    expect(capture.bannerVisible()).toBe(true);
    nextResult = realFaceResult(2);
    await frameAt(5300);
    await frameAt(5900); // the face was back for more than 500 ms
    expect(events.map((e) => e.type)).toEqual(["face_lost", "face_regained"]);
    expect(events[1]?.data).toEqual({ lost_ms: 3200 });
    expect(capture.bannerVisible()).toBe(false);
    await capture.close();
  });

  it("the playing video's fps becomes the model's target rate, capped at the camera's", async () => {
    const capture = await Capture.open(CONFIG, sinks(), makeParts());
    expect(capture.rate.currentFps()).toBe(30);
    capture.setPlaybackFps(25);
    expect(capture.rate.currentFps()).toBe(25);
    capture.setPlaybackFps(60);
    expect(capture.rate.currentFps()).toBe(25); // never raised by a target alone, but capped below
    capture.setPlaybackFps(null);
    await capture.close();
  });

  it("a lost camera is fatal", async () => {
    const capture = await Capture.open(CONFIG, sinks(), makeParts());
    endedCallbacks[0]?.();
    expect(fatals.map((e) => e.message)).toEqual(["The camera stopped working during the study."]);
    await capture.close();
  });

  it("close stops the recorder, the model and the camera, once", async () => {
    const capture = await Capture.open(CONFIG, sinks(), makeParts());
    capture.startRecording();
    await capture.close();
    await capture.close();
    expect([recorderStops, modelClosed, cameraStopped]).toEqual([1, 1, 1]);
    endedCallbacks[0]?.(); // a camera that ends after close is not an error
    expect(fatals).toEqual([]);
  });

  it("a failure while starting the tracker releases the camera and the model", async () => {
    await expect(
      Capture.open(
        CONFIG,
        sinks(),
        makeParts({
          createVideo: () => {
            const video = document.createElement("video");
            video.play = () => Promise.reject(new Error("play refused"));
            return video;
          },
        }),
      ),
    ).rejects.toThrow("play refused");
    expect([modelClosed, cameraStopped]).toEqual([1, 1]);
  });

  it("a camera without a reported frame rate is assumed to deliver the study's, and says so in the events", async () => {
    const parts = makeParts();
    const camera = await parts.requestCamera(CONFIG.recording);
    const noRate: CameraHandle = { ...camera, settings: { width: 854, height: 480 } };
    const capture = await Capture.open(CONFIG, sinks(), {
      ...parts,
      requestCamera: async () => noRate,
    });
    expect(capture.rate.currentFps()).toBe(CONFIG.recording.fps);
    expect(events.map((e) => e.type)).toEqual(["client_error"]);
    expect(String(events[0]?.data.message)).toContain("no frame rate");
    await capture.close();
  });

  it("a failing stop of the recording during close is logged, and the camera still goes off", async () => {
    const parts = makeParts({
      startRecorder: () => ({
        mimeType: "video/webm",
        stop: () => Promise.reject(new Error("disk full")),
      }),
    });
    const capture = await Capture.open(CONFIG, sinks(), parts);
    capture.startRecording();
    await capture.close();
    expect(cameraStopped).toBe(1);
    expect(events.map((e) => e.type)).toEqual(["client_error"]);
    expect(String(events[0]?.data.message)).toContain("disk full");
  });

  it("a camera error rejects open; a model error stops the camera again", async () => {
    await expect(
      Capture.open(
        CONFIG,
        sinks(),
        makeParts({ requestCamera: () => Promise.reject(new Error("no camera")) }),
      ),
    ).rejects.toThrow("no camera");
    await expect(
      Capture.open(
        CONFIG,
        sinks(),
        makeParts({ startModel: () => Promise.reject(new Error("no model")) }),
      ),
    ).rejects.toThrow("no model");
    expect(cameraStopped).toBe(1);
  });

  it("a frame that cannot be processed is fatal (fail loudly)", async () => {
    const capture = await Capture.open(
      CONFIG,
      sinks(),
      makeParts({
        startModel: async () => ({
          detect: () => Promise.reject(new Error("model crashed")),
          close: () => undefined,
        }),
      }),
    );
    await frameAt(1100);
    expect(fatals.map((e) => e.message)).toEqual(["model crashed"]);
    await capture.close();
  });
});
