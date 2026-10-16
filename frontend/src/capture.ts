// The live capture of a session (PLAN §5.3, §5.4): camera, face model, live tracking and the
// recorder, with everything they produce routed to the right stream. Opened at the device check;
// recording (and the feature stream) starts at the fullscreen step and ends before the upload.

import { now } from "./clock";
import type { StudyConfig } from "./config";
import { Ema, RateCounter } from "./debug/counters";
import type { EventLog } from "./eventLog";
import { AdaptiveRate, FrameGate, targetFps } from "./face/adaptiveRate";
import { FrameBuffer, gateFrame } from "./face/calibrationGate";
import { FaceModel } from "./face/faceModel";
import { FaceLostTracker } from "./face/faceLost";
import { FaceTracker, type FaceTrackerOptions, type TrackedFrame } from "./face/faceTracker";
import { type CameraHandle, requestCamera } from "./media/camera";
import { type RecorderHandle, startRecorder } from "./media/recorder";
import type { FeatureRecord } from "./records";

export interface CaptureSinks {
  log: EventLog;
  /** Feature records go here while recording. */
  pushFeature: (record: FeatureRecord) => void;
  /** One webcam chunk of the recording; resolves with its seq. */
  enqueueWebcam: (bytes: Uint8Array) => Promise<number>;
  /** A loud, unrecoverable failure (camera lost, recorder or queue failed). */
  onFatal: (error: Error) => void;
}

/** What `Capture` is built from; the tests replace the browser parts. */
export interface CaptureParts {
  requestCamera: typeof requestCamera;
  startModel: () => Promise<Pick<FaceModel, "detect" | "close">>;
  startRecorder: typeof startRecorder;
  createVideo: () => HTMLVideoElement;
  /** How a frame is grabbed for the model and the brightness (tests have no canvas). */
  frameIo?: Pick<FaceTrackerOptions, "grabPixels" | "grabBitmap">;
}

const realParts: CaptureParts = {
  requestCamera,
  startModel: () => FaceModel.start(),
  startRecorder,
  createVideo: () => {
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    // In the page but invisible: some browsers do not run frame callbacks for a detached element.
    Object.assign(video.style, {
      position: "fixed",
      width: "1px",
      height: "1px",
      opacity: "0",
      pointerEvents: "none",
    });
    document.body.append(video);
    return video;
  },
};

export class Capture {
  /** Frames of the live stream for the calibration gate. */
  readonly frames = new FrameBuffer();
  readonly rate: AdaptiveRate;
  readonly faceLost = new FaceLostTracker();
  /** Counters for the debug panel. */
  readonly webcamFps = new RateCounter(now);
  readonly modelFps = new RateCounter(now);
  readonly inferenceMs = new Ema();
  /** Every processed frame, for the device check and the debug panel. */
  readonly listeners = new Set<(frame: TrackedFrame) => void>();
  droppedStimulusFrames = 0;
  lastFrame: TrackedFrame | null = null;

  private recording = false;
  private recorder: RecorderHandle | null = null;
  private tracker: FaceTracker | null = null;
  private readonly cameraFps: number;
  private closed = false;

  private constructor(
    private readonly config: StudyConfig,
    private readonly sinks: CaptureSinks,
    private readonly parts: CaptureParts,
    readonly camera: CameraHandle,
    private readonly model: Pick<FaceModel, "detect" | "close">,
    readonly video: HTMLVideoElement,
  ) {
    this.cameraFps = camera.settings.frameRate ?? config.recording.fps;
    this.rate = new AdaptiveRate({ targetFps: targetFps(null, this.cameraFps) });
  }

  /** Ask for the camera, load the face model and start tracking. Rejects with a CameraError first. */
  static async open(
    config: StudyConfig,
    sinks: CaptureSinks,
    parts: CaptureParts = realParts,
  ): Promise<Capture> {
    const camera = await parts.requestCamera(config.recording);
    let model: Pick<FaceModel, "detect" | "close">;
    try {
      model = await parts.startModel();
    } catch (e) {
      camera.stop();
      throw e;
    }
    const video = parts.createVideo();
    video.srcObject = camera.stream;
    const capture = new Capture(config, sinks, parts, camera, model, video);
    camera.onEnded(() => {
      if (!capture.closed) sinks.onFatal(new Error("The camera stopped working during the study."));
    });
    await video.play();
    capture.tracker = new FaceTracker({
      video,
      model,
      gate: new FrameGate(capture.rate),
      onFrame: (frame) => capture.onFrame(frame),
      onWebcamFrame: () => capture.webcamFps.tick(),
      onError: (error) => sinks.onFatal(error),
      ...parts.frameIo,
    });
    capture.tracker.start();
    return capture;
  }

  /** The camera stream (for the device-check preview). */
  get stream(): MediaStream {
    return this.camera.stream;
  }

  /** The face model's target rate: the playing video's fps (null: the camera's), PLAN §5.4. */
  setPlaybackFps(videoFps: number | null): void {
    this.rate.setTarget(targetFps(videoFps, this.cameraFps));
  }

  /** Start the webcam recording and the feature stream (PLAN §5.2 step 6). */
  startRecording(): void {
    if (this.recording) return;
    this.recording = true;
    this.recorder = this.parts.startRecorder({
      stream: this.camera.stream,
      settings: { ...this.camera.settings },
      log: this.sinks.log,
      enqueue: this.sinks.enqueueWebcam,
      timesliceMs: this.config.recording.chunk_ms,
      onError: this.sinks.onFatal,
    });
  }

  /** Stop recording after the last chunk; the tracker keeps running until `close`. */
  async stopRecording(): Promise<void> {
    this.recording = false;
    await this.recorder?.stop();
    this.recorder = null;
  }

  /** True while the "we cannot see your face" banner should show (PLAN §5.2). */
  bannerVisible(): boolean {
    return this.recording && this.faceLost.bannerVisible(now());
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    if (this.recording) await this.stopRecording().catch(() => undefined);
    this.tracker?.stop();
    this.model.close();
    this.camera.stop();
    this.video.srcObject = null;
    this.video.remove();
  }

  private onFrame(frame: TrackedFrame): void {
    this.lastFrame = frame;
    this.modelFps.tick();
    this.inferenceMs.add(frame.inferenceMs);
    this.frames.push(gateFrame(frame.t, frame.features, frame.result, frame.brightness));
    if (this.recording) {
      this.sinks.pushFeature(frame.record);
      for (const event of this.faceLost.update(frame.t, frame.features !== null)) {
        if (event.type === "face_lost") this.sinks.log.log("face_lost", event.data);
        else this.sinks.log.log("face_regained", event.data);
      }
    }
    for (const listener of this.listeners) listener(frame);
  }
}
