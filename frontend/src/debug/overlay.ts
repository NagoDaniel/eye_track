// The debug overlay panel (PLAN §12.1): webcam preview with landmarks, live counters, quality
// signals, the last events, the calibration gate numbers and the quality threshold sliders.
// Created only by the caller (which checks `debugRequested` and `debug_allowed`). It takes plain
// data through `update(view)`; it does not know the camera, the face model or the gate.

import {
  type FaceDrawing,
  type GateErrorPoint,
  type PixelSize,
  drawFaceOverlay,
  drawGateErrors,
} from "./drawing";
import {
  QUALITY_SLIDERS,
  type QualityKey,
  type QualityThresholds,
  isValidQuality,
  qualityToJson,
} from "./qualityExport";

export const PANEL_WIDTH_PX = 340;
export const PREVIEW_WIDTH_PX = 320;
export const PREVIEW_DEFAULT_HEIGHT_PX = 240;
const PANEL_Z_INDEX = "2147483600";
const CANVAS_Z_INDEX = "2147483500";
const MISSING = "–";

export interface DebugCounters {
  webcamFps: number | null;
  faceModelFps: number | null;
  inferenceMs: number | null;
  droppedStimulusFrames: number | null;
  uploadQueueChunks: number | null;
  uploadPendingBytes: number | null;
}

export interface QualitySignals {
  brightness: number | null;
  /** Interocular distance as a fraction of the frame width. */
  faceRatio: number | null;
  /** Current face size divided by the face size at calibration. */
  faceScaleRatio: number | null;
  yawDeg: number | null;
  pitchDeg: number | null;
  rollDeg: number | null;
  blink: boolean | null;
}

export interface GateView {
  attempt: number;
  passed: boolean | null;
  meanErrorNorm: number | null;
  pointErrors: readonly number[];
  validFramesPerDot: readonly number[];
  reason: string;
}

export interface DebugView {
  webcam: HTMLVideoElement | null;
  face: FaceDrawing | null;
  counters: DebugCounters;
  signals: QualitySignals;
  /** From `EventTail.lines()`. */
  events: readonly string[];
  gate: GateView | null;
  /** Targets and median predictions of the validation block, drawn over the whole viewport. */
  gateErrors: readonly GateErrorPoint[] | null;
  /** The thresholds currently in force. */
  quality: QualityThresholds;
}

export interface DebugOverlayCallbacks {
  /** Called with the full new thresholds, only when they are valid. */
  onQualityChange(quality: QualityThresholds): void;
}

export interface DebugOverlayOptions {
  /** `study_config_default.quality` of shared/protocol.json; the JSON export lists only the differences. */
  qualityDefaults: QualityThresholds;
}

export interface DebugOverlay {
  update(view: DebugView): void;
  setVisible(visible: boolean): void;
  destroy(): void;
}

// --- formatting ---

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

function num(value: number | null, digits: number, suffix = ""): string {
  return value === null ? MISSING : `${value.toFixed(digits)}${suffix}`;
}

export function formatCounters(c: DebugCounters): string {
  const queue =
    c.uploadQueueChunks === null
      ? MISSING
      : `${c.uploadQueueChunks} chunks, ${c.uploadPendingBytes === null ? MISSING : formatBytes(c.uploadPendingBytes)}`;
  return [
    `webcam fps      ${num(c.webcamFps, 1)}`,
    `face model fps  ${num(c.faceModelFps, 1)}`,
    `inference       ${num(c.inferenceMs, 1, " ms")}`,
    `dropped frames  ${c.droppedStimulusFrames ?? MISSING}`,
    `upload queue    ${queue}`,
  ].join("\n");
}

export function formatSignals(s: QualitySignals): string {
  const blink = s.blink === null ? MISSING : s.blink ? "yes" : "no";
  return [
    `brightness  ${num(s.brightness, 1)}`,
    `face ratio  ${num(s.faceRatio, 3)}`,
    `face scale  ${num(s.faceScaleRatio, 2)}`,
    `yaw         ${num(s.yawDeg, 1, "°")}`,
    `pitch       ${num(s.pitchDeg, 1, "°")}`,
    `roll        ${num(s.rollDeg, 1, "°")}`,
    `blink       ${blink}`,
  ].join("\n");
}

export function formatGate(g: GateView | null): string {
  if (g === null) return "no gate result yet";
  const verdict = g.passed === null ? MISSING : g.passed ? "PASSED" : "FAILED";
  return [
    `attempt ${g.attempt}: ${verdict}`,
    `mean error ${num(g.meanErrorNorm, 3)}`,
    `point errors ${g.pointErrors.map((e) => e.toFixed(3)).join(" ") || MISSING}`,
    `valid frames ${g.validFramesPerDot.join(" ") || MISSING}`,
    `reason: ${g.reason || MISSING}`,
  ].join("\n");
}

/** The measured value that a threshold is compared with. */
const MEASURED: Record<QualityKey, (s: QualitySignals) => number | null> = {
  min_face_ratio_warn: (s) => s.faceScaleRatio,
  max_face_ratio_warn: (s) => s.faceScaleRatio,
  brightness_min: (s) => s.brightness,
  brightness_max: (s) => s.brightness,
  face_ratio_min: (s) => s.faceRatio,
  face_ratio_max: (s) => s.faceRatio,
  max_yaw_deg: (s) => (s.yawDeg === null ? null : Math.abs(s.yawDeg)),
  max_pitch_deg: (s) => (s.pitchDeg === null ? null : Math.abs(s.pitchDeg)),
};

// --- DOM ---

const STYLE = `
.debug-panel { position: fixed; top: 8px; right: 8px; width: ${PANEL_WIDTH_PX}px; max-height: calc(100vh - 16px);
  overflow-y: auto; box-sizing: border-box; padding: 8px; background: rgba(10, 10, 14, 0.82); color: #e8e8ee;
  font: 11px/1.35 ui-monospace, Menlo, Consolas, monospace; border: 1px solid #ff5d5d; border-radius: 6px;
  z-index: ${PANEL_Z_INDEX}; pointer-events: auto; text-align: left; }
.debug-panel h2 { margin: 0 0 4px; font-size: 13px; color: #ff5d5d; letter-spacing: 0.1em; }
.debug-panel h3 { margin: 8px 0 2px; font-size: 11px; color: #9aa0b4; text-transform: uppercase; }
.debug-panel pre { margin: 0; white-space: pre-wrap; word-break: break-all; font: inherit; }
.debug-preview { position: relative; width: ${PREVIEW_WIDTH_PX}px; }
.debug-preview video, .debug-preview canvas { position: absolute; left: 0; top: 0; width: 100%; height: 100%; }
.debug-slider { display: grid; grid-template-columns: 1fr; margin-bottom: 4px; }
.debug-slider input { width: 100%; }
.debug-slider.invalid .debug-slider-text { color: #ff5d5d; }
.debug-panel button { font: inherit; margin-top: 4px; }
.debug-panel textarea { width: 100%; box-sizing: border-box; height: 90px; font: inherit; }
.debug-gate-canvas { position: fixed; left: 0; top: 0; pointer-events: none; z-index: ${CANVAS_Z_INDEX}; }
`;

function node<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const n = document.createElement(tag);
  if (className !== undefined) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}

function section(panel: HTMLElement, title: string, name: string): HTMLPreElement {
  panel.append(node("h3", undefined, title));
  const pre = node("pre");
  pre.dataset["debug"] = name;
  panel.append(pre);
  return pre;
}

interface SliderRow {
  key: QualityKey;
  input: HTMLInputElement;
  text: HTMLElement;
  wrap: HTMLElement;
}

export function createDebugOverlay(
  root: HTMLElement,
  callbacks: DebugOverlayCallbacks,
  options: DebugOverlayOptions,
): DebugOverlay {
  const wrapper = node("div", "debug-root");
  const style = node("style", undefined, STYLE);
  const gateCanvas = node("canvas", "debug-gate-canvas");
  const panel = node("div", "debug-panel");
  wrapper.append(style, gateCanvas, panel);

  panel.append(node("h2", undefined, "DEBUG"));
  panel.append(node("div", undefined, "D overlay · G gaze dot · S skip · F force gate fail"));

  panel.append(node("h3", undefined, "Webcam"));
  const preview = node("div", "debug-preview");
  preview.style.height = `${PREVIEW_DEFAULT_HEIGHT_PX}px`;
  const faceCanvas = node("canvas");
  faceCanvas.style.pointerEvents = "none";
  faceCanvas.width = PREVIEW_WIDTH_PX;
  faceCanvas.height = PREVIEW_DEFAULT_HEIGHT_PX;
  preview.append(faceCanvas);
  panel.append(preview);

  const countersPre = section(panel, "Counters", "counters");
  const signalsPre = section(panel, "Quality signals", "signals");
  const gatePre = section(panel, "Calibration gate", "gate");
  const eventsPre = section(panel, "Last events", "events");

  panel.append(node("h3", undefined, "Quality thresholds"));
  const rows: SliderRow[] = [];
  let latest: DebugView | null = null;
  let current: QualityThresholds = { ...options.qualityDefaults };

  const sliderValues = (): QualityThresholds => {
    const q = { ...current };
    for (const row of rows) q[row.key] = Number(row.input.value);
    return q;
  };

  for (const slider of QUALITY_SLIDERS) {
    const wrap = node("div", "debug-slider");
    const text = node("span", "debug-slider-text");
    const input = node("input");
    input.type = "range";
    input.min = String(slider.min);
    input.max = String(slider.max);
    input.step = String(slider.step);
    input.value = String(current[slider.key]);
    input.dataset["key"] = slider.key;
    input.addEventListener("input", () => {
      const candidate = sliderValues();
      const valid = isValidQuality(candidate);
      wrap.classList.toggle("invalid", !valid);
      if (valid) {
        current = candidate;
        callbacks.onQualityChange(candidate);
      }
      renderSliderText();
    });
    wrap.append(text, input);
    panel.append(wrap);
    rows.push({ key: slider.key, input, text, wrap });
  }

  function renderSliderText(): void {
    for (const row of rows) {
      const slider = QUALITY_SLIDERS.find((s) => s.key === row.key);
      const measured = latest === null ? null : MEASURED[row.key](latest.signals);
      row.text.textContent = `${slider?.label ?? row.key}: ${row.input.value}  (now ${num(measured, 2)})`;
    }
  }

  const copyButton = node("button", undefined, "Copy as JSON");
  copyButton.type = "button";
  const copyStatus = node("div", "debug-copy-status");
  const fallback = node("textarea");
  fallback.readOnly = true;
  fallback.hidden = true;
  fallback.dataset["debug"] = "json-fallback";
  copyButton.addEventListener("click", () => {
    const json = qualityToJson(sliderValues(), options.qualityDefaults);
    const showFallback = (): void => {
      fallback.value = json;
      fallback.hidden = false;
      fallback.select();
      copyStatus.textContent = "Clipboard unavailable: copy the text below.";
    };
    const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
    if (clipboard === undefined) {
      showFallback();
      return;
    }
    clipboard.writeText(json).then(() => {
      fallback.hidden = true;
      copyStatus.textContent = "Copied.";
    }, showFallback);
  });
  panel.append(copyButton, copyStatus, fallback);

  root.append(wrapper);

  let mountedVideo: HTMLVideoElement | null = null;

  function syncVideo(video: HTMLVideoElement | null): void {
    if (video === mountedVideo) return;
    mountedVideo?.remove();
    mountedVideo = video;
    if (video !== null) preview.prepend(video);
  }

  function previewSize(video: HTMLVideoElement | null): PixelSize {
    const ratio =
      video !== null && video.videoWidth > 0 ? video.videoHeight / video.videoWidth : null;
    const height =
      ratio === null ? PREVIEW_DEFAULT_HEIGHT_PX : Math.round(PREVIEW_WIDTH_PX * ratio);
    return { width: PREVIEW_WIDTH_PX, height };
  }

  function resize(canvas: HTMLCanvasElement, size: PixelSize): void {
    if (canvas.width !== size.width) canvas.width = size.width;
    if (canvas.height !== size.height) canvas.height = size.height;
  }

  return {
    update(view) {
      latest = view;
      syncVideo(view.webcam);

      const size = previewSize(view.webcam);
      resize(faceCanvas, size);
      preview.style.height = `${size.height}px`;
      const faceCtx = faceCanvas.getContext("2d");
      if (faceCtx !== null) drawFaceOverlay(faceCtx, view.face, size);

      const viewport = { width: window.innerWidth, height: window.innerHeight };
      resize(gateCanvas, viewport);
      const gateCtx = gateCanvas.getContext("2d");
      if (gateCtx !== null) drawGateErrors(gateCtx, view.gateErrors ?? [], viewport);

      countersPre.textContent = formatCounters(view.counters);
      signalsPre.textContent = formatSignals(view.signals);
      gatePre.textContent = formatGate(view.gate);
      eventsPre.textContent = view.events.join("\n");

      current = { ...view.quality };
      for (const row of rows) {
        // A slider being dragged keeps the user's value until released.
        if (document.activeElement !== row.input) {
          row.input.value = String(view.quality[row.key]);
          row.wrap.classList.remove("invalid");
        }
      }
      renderSliderText();
    },
    setVisible(visible) {
      wrapper.hidden = !visible;
    },
    destroy() {
      wrapper.remove();
    },
  };
}
