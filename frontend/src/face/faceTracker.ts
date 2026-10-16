// Runs the face model on the live webcam (PLAN §5.3, §5.4): for the frames the rate gate lets
// through it grabs the frame, runs the model in the worker, measures the brightness of the face
// region and hands the result on. Frames that arrive while the model is busy are skipped, so the
// main thread never queues work (playback smoothness has priority).

import { now } from "../clock";
import type { FeatureRecord } from "../records";
import { type FrameFeatures, frameFeatures } from "./features";
import {
  type FaceResult,
  type RgbaImage,
  buildFeatureRecord,
  faceBoxOf,
  meanLuminance,
  subsetOf,
} from "./featureRecord";
import type { ModelOutput } from "./faceModel";

/** Width of the downscaled frame the brightness is measured on; plenty for a mean. */
export const BRIGHTNESS_SAMPLE_WIDTH = 160;

export interface TrackedFrame {
  /** Client clock when the webcam frame was presented (PLAN §5.3), also the record's `t`. */
  t: number;
  result: FaceResult;
  /** The Pipeline A features; null without a face. */
  features: FrameFeatures | null;
  brightness: number | null;
  inferenceMs: number;
  aspect: number;
  record: FeatureRecord;
}

export interface TrackerStats {
  /** Webcam frames seen and frames the model processed since the tracker started. */
  webcamFrames: number;
  modelFrames: number;
  /** Frames dropped because the model was still busy. */
  skippedBusy: number;
}

export interface FaceTrackerOptions {
  video: HTMLVideoElement;
  model: { detect(bitmap: ImageBitmap, timestampMs: number): Promise<ModelOutput> };
  gate: { shouldProcess(t: number): boolean };
  onFrame: (frame: TrackedFrame) => void;
  /** A frame that could not be processed; the tracker carries on with the next one. */
  onError: (error: Error) => void;
  /** Called for every webcam frame, processed or not (webcam fps counter). */
  onWebcamFrame?: (t: number) => void;
  clock?: () => number;
  grabPixels?: (video: HTMLVideoElement) => RgbaImage | null;
  grabBitmap?: (video: HTMLVideoElement) => Promise<ImageBitmap>;
}

function canvasGrabber(): (video: HTMLVideoElement) => RgbaImage | null {
  let canvas: HTMLCanvasElement | null = null;
  return (video) => {
    if (video.videoWidth === 0 || video.videoHeight === 0) return null;
    canvas ??= document.createElement("canvas");
    const width = BRIGHTNESS_SAMPLE_WIDTH;
    const height = Math.max(1, Math.round((width * video.videoHeight) / video.videoWidth));
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (context === null) return null;
    context.drawImage(video, 0, 0, width, height);
    return context.getImageData(0, 0, width, height);
  };
}

export class FaceTracker {
  readonly stats: TrackerStats = { webcamFrames: 0, modelFrames: 0, skippedBusy: 0 };
  private busy = false;
  private handle: number | null = null;
  private running = false;
  private readonly clock: () => number;
  private readonly grabPixels: (video: HTMLVideoElement) => RgbaImage | null;
  private readonly grabBitmap: (video: HTMLVideoElement) => Promise<ImageBitmap>;

  constructor(private readonly options: FaceTrackerOptions) {
    if (typeof options.video.requestVideoFrameCallback !== "function") {
      throw new Error("requestVideoFrameCallback is not available");
    }
    this.clock = options.clock ?? now;
    this.grabPixels = options.grabPixels ?? canvasGrabber();
    this.grabBitmap = options.grabBitmap ?? ((video) => createImageBitmap(video));
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.arm();
  }

  stop(): void {
    this.running = false;
    if (this.handle !== null) this.options.video.cancelVideoFrameCallback(this.handle);
    this.handle = null;
  }

  private arm(): void {
    this.handle = this.options.video.requestVideoFrameCallback((_now, metadata) => {
      this.handle = null;
      if (!this.running) return;
      this.arm(); // first: a failure below must not stop the loop
      this.onVideoFrame(metadata);
    });
  }

  private onVideoFrame(metadata: VideoFrameCallbackMetadata): void {
    const t = this.clock();
    this.stats.webcamFrames++;
    this.options.onWebcamFrame?.(t);
    if (!this.options.gate.shouldProcess(t)) return;
    if (this.busy) {
      this.stats.skippedBusy++;
      return;
    }
    this.busy = true;
    this.process(t, metadata)
      .catch((e: unknown) => this.options.onError(e instanceof Error ? e : new Error(String(e))))
      .finally(() => {
        this.busy = false;
      });
  }

  private async process(t: number, metadata: VideoFrameCallbackMetadata): Promise<void> {
    const { video } = this.options;
    const aspect = video.videoWidth / video.videoHeight;
    const pixels = this.grabPixels(video); // synchronous: the frame the model sees
    const bitmap = await this.grabBitmap(video);
    const { result, inferenceMs } = await this.options.model.detect(bitmap, t);
    this.stats.modelFrames++;
    const hasFace = result.faces > 0 && result.landmarks !== null && result.matrix !== null;
    const brightness =
      hasFace && pixels !== null && result.landmarks !== null
        ? meanLuminance(pixels, faceBoxOf(result.landmarks))
        : null;
    const record = buildFeatureRecord({
      t,
      webcamMediaTime: metadata.mediaTime,
      captureTime: metadata.captureTime ?? null,
      presentedFrames: metadata.presentedFrames,
      result,
      aspect,
      brightness,
    });
    const features =
      hasFace && result.landmarks !== null && result.matrix !== null
        ? frameFeatures(subsetOf(result.landmarks), result.matrix, aspect)
        : null;
    this.options.onFrame({ t, result, features, brightness, inferenceMs, aspect, record });
  }
}
