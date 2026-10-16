// The participant debug mode wired together (PLAN §12.1): overlay, keyboard shortcuts, live gaze
// dot, event tail and skipping. Created only when debug mode is on (`?debug=1` and allowed by the
// server); it never runs for a normal participant.

import type { StudyConfig } from "../config";
import type { EventLog, EventSink, SkipTarget } from "../eventLog";
import type { GateReport } from "../face/calibrationGate";
import type { TrackedFrame } from "../face/faceTracker";
import { faceBoxOf } from "../face/featureRecord";
import { featureVector, isBlink } from "../face/quickCalib";
import { type RidgeModel, predict } from "../face/ridge";
import { type DebugState, debugKeyAction, initialDebugState, reduceDebug } from "./debugMode";
import type { FaceDrawing, NormPoint } from "./drawing";
import { EventTail } from "./eventTail";
import { createGazeDot } from "./gazeDot";
import { type DebugOverlay, type DebugView, createDebugOverlay } from "./overlay";
import type { QualityThresholds } from "./qualityExport";
import { OneEuroFilter2D } from "./oneEuro";
import { SkipBus } from "./skip";

/** How often the panel is redrawn. */
export const OVERLAY_UPDATE_MS = 250;
const RIGHT_IRIS = [468, 469, 470, 471, 472] as const;
const LEFT_IRIS = [473, 474, 475, 476, 477] as const;

/** What the debug mode reads from the running capture. */
export interface DebugCapture {
  stream: MediaStream;
  lastFrame: TrackedFrame | null;
  webcamFps: { rate(): number };
  modelFps: { rate(): number };
  inferenceMs: { value: number | null };
  droppedStimulusFrames: number;
  listeners: Set<(frame: TrackedFrame) => void>;
}

export interface DebugGate {
  model: RidgeModel | null;
  lastReport: GateReport | null;
}

export interface DebugSessionDeps {
  root: HTMLElement;
  config: StudyConfig;
  log: EventLog;
  eventSinks: Set<EventSink>;
  queueStats: () => { pendingChunks: number; pendingBytes: number };
  /** `study_config_default.quality` of shared/protocol.json. */
  qualityDefaults: QualityThresholds;
  doc?: Document;
  win?: Window;
  /** The webcam preview element (tests have no media). */
  createPreview?: () => HTMLVideoElement;
}

function irisCentre(mesh: readonly NormPoint[], indices: readonly number[]): NormPoint {
  let x = 0;
  let y = 0;
  for (const i of indices) {
    x += mesh[i]?.x ?? 0;
    y += mesh[i]?.y ?? 0;
  }
  return { x: x / indices.length, y: y / indices.length };
}

export class DebugSession {
  readonly skipBus = new SkipBus();
  private state: DebugState = initialDebugState();
  private readonly tail = new EventTail();
  private readonly tailSink: EventSink = (record) => this.tail.push(record);
  private readonly overlay: DebugOverlay;
  private readonly gazeDot: ReturnType<typeof createGazeDot>;
  private readonly doc: Document;
  private readonly win: Window;
  private capture: DebugCapture | null = null;
  private gate: DebugGate | null = null;
  private preview: HTMLVideoElement | null = null;
  private referenceFaceRatio: number | null = null;
  private target: SkipTarget | null = null;
  private readonly smoother = new OneEuroFilter2D();
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly onFrame = (frame: TrackedFrame): void => this.moveGazeDot(frame);
  private readonly onKey = (event: KeyboardEvent): void => {
    const tag = event.target instanceof Element ? event.target.tagName : null;
    const action = debugKeyAction(event, tag);
    if (action === null) return;
    const before = this.state;
    this.state = reduceDebug(this.state, action);
    this.overlay.setVisible(this.state.overlayVisible);
    if (!this.state.gazeDotVisible) this.gazeDot.hide();
    if (this.state.skipRequested !== before.skipRequested && this.target !== null) {
      this.deps.log.log("debug_skip", { target: this.target });
      this.skipBus.fire();
    }
  };

  constructor(private readonly deps: DebugSessionDeps) {
    this.doc = deps.doc ?? document;
    this.win = deps.win ?? window;
    this.overlay = createDebugOverlay(
      deps.root,
      {
        // The thresholds in force are the config's own object, so the device check and the gate
        // see a change at once (PLAN §12.1; DECISIONS milestone 2 item 3).
        onQualityChange: (quality) => Object.assign(deps.config.quality, quality),
      },
      { qualityDefaults: deps.qualityDefaults },
    );
    this.gazeDot = createGazeDot(deps.root);
    deps.eventSinks.add(this.tailSink);
    this.doc.addEventListener("keydown", this.onKey);
    this.timer = setInterval(() => this.overlay.update(this.view()), OVERLAY_UPDATE_MS);
    this.overlay.update(this.view());
  }

  /** The camera is open (called from the device check). */
  attach(capture: DebugCapture): void {
    this.capture = capture;
    capture.listeners.add(this.onFrame);
    const create =
      this.deps.createPreview ??
      ((): HTMLVideoElement => {
        const video = this.doc.createElement("video");
        video.muted = true;
        video.autoplay = true;
        video.playsInline = true;
        return video;
      });
    this.preview = create();
    this.preview.srcObject = capture.stream;
  }

  setGate(gate: DebugGate): void {
    this.gate = gate;
  }

  /** The median face ratio of the device check; the panel shows the current size relative to it. */
  setReferenceFaceRatio(ratio: number | null): void {
    this.referenceFaceRatio = ratio;
  }

  /** What `S` skips right now (null: nothing, e.g. on a text screen). */
  setTarget(target: SkipTarget | null): void {
    this.target = target;
  }

  /** `F`: the calibration gate fails on purpose. */
  forceFailure = (): boolean => this.state.forceGateFailure;

  destroy(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    this.doc.removeEventListener("keydown", this.onKey);
    this.capture?.listeners.delete(this.onFrame);
    this.deps.eventSinks.delete(this.tailSink);
    if (this.preview !== null) this.preview.srcObject = null;
    this.overlay.destroy();
    this.gazeDot.destroy();
  }

  /**
   * The live in-browser gaze (PLAN §12.1): the quick model on the latest frame, smoothed (one-euro
   * filter) and kept on the screen, so a wild prediction cannot leave the viewport.
   */
  private moveGazeDot(frame: TrackedFrame): void {
    const model = this.gate?.model ?? null;
    if (!this.state.gazeDotVisible || model === null || frame.features === null) {
      this.smoother.reset();
      this.gazeDot.hide();
      return;
    }
    const [x, y] = predict(model, featureVector(frame.features));
    const [sx, sy] = this.smoother.filter(frame.t, x, y);
    this.gazeDot.show(
      Math.min(Math.max(sx, 0), this.win.innerWidth),
      Math.min(Math.max(sy, 0), this.win.innerHeight),
    );
  }

  private view(): DebugView {
    const capture = this.capture;
    const frame = capture?.lastFrame ?? null;
    const features = frame?.features ?? null;
    const queue = this.deps.queueStats();
    const report = this.gate?.lastReport ?? null;
    const mesh: NormPoint[] | null = frame?.result.landmarks?.map(([x, y]) => ({ x, y })) ?? null;
    const face: FaceDrawing | null =
      mesh === null || frame?.result.landmarks == null
        ? null
        : {
            landmarks: mesh,
            irisCenters: [irisCentre(mesh, RIGHT_IRIS), irisCentre(mesh, LEFT_IRIS)],
            box: faceBoxOf(frame.result.landmarks),
          };
    const faceRatio = features?.faceRatio ?? null;
    return {
      webcam: this.preview,
      face,
      counters: {
        webcamFps: capture?.webcamFps.rate() ?? null,
        faceModelFps: capture?.modelFps.rate() ?? null,
        inferenceMs: capture?.inferenceMs.value ?? null,
        droppedStimulusFrames: capture?.droppedStimulusFrames ?? null,
        uploadQueueChunks: queue.pendingChunks,
        uploadPendingBytes: queue.pendingBytes,
      },
      signals: {
        brightness: frame?.brightness ?? null,
        faceRatio,
        faceScaleRatio:
          faceRatio !== null && this.referenceFaceRatio !== null && this.referenceFaceRatio > 0
            ? faceRatio / this.referenceFaceRatio
            : null,
        yawDeg: features?.yaw ?? null,
        pitchDeg: features?.pitch ?? null,
        rollDeg: features?.roll ?? null,
        blink:
          frame === null || features === null
            ? null
            : isBlink(frame.result.blinkL, frame.result.blinkR, features.openL, features.openR),
      },
      events: this.tail.lines(),
      gate:
        report === null
          ? null
          : {
              attempt: report.attempt,
              passed: report.decision.passed,
              meanErrorNorm: report.measured.meanErrorNorm,
              pointErrors: report.measured.pointErrors.map((e) => e ?? Number.NaN),
              validFramesPerDot: report.measured.validFramesPerDot,
              reason: report.decision.reason,
              model:
                report.choice === null
                  ? null
                  : `${report.choice.degree === 1 ? "linear" : "degree 2"}, alpha ${report.choice.alpha}` +
                    (report.choice.cvErrorPx === null
                      ? ""
                      : `, CV error ${Math.round(report.choice.cvErrorPx)} px`),
            },
      gateErrors:
        report === null
          ? null
          : report.points.map((p) => {
              const w = this.win.innerWidth;
              const h = this.win.innerHeight;
              return {
                target: { x: p.target.x / w, y: p.target.y / h },
                prediction:
                  p.prediction === null ? null : { x: p.prediction.x / w, y: p.prediction.y / h },
              };
            }),
      quality: { ...this.deps.config.quality },
    };
  }
}
