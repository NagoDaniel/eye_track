// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { QualityThresholds } from "./qualityExport";
import { type DebugView, createDebugOverlay, formatBytes } from "./overlay";

const defaults: QualityThresholds = {
  min_face_ratio_warn: 0.8,
  max_face_ratio_warn: 1.25,
  brightness_min: 0.25,
  brightness_max: 0.85,
  face_ratio_min: 0.1,
  face_ratio_max: 0.25,
  max_yaw_deg: 25,
  max_pitch_deg: 20,
};

function view(overrides: Partial<DebugView> = {}): DebugView {
  return {
    webcam: null,
    face: null,
    counters: {
      webcamFps: 29.8,
      faceModelFps: 14.2,
      inferenceMs: 23.456,
      droppedStimulusFrames: 7,
      uploadQueueChunks: 3,
      uploadPendingBytes: 1536 * 1024,
    },
    signals: {
      brightness: 0.47,
      faceRatio: 0.153,
      faceScaleRatio: 1.02,
      yawDeg: -3.5,
      pitchDeg: 1.25,
      rollDeg: 0.5,
      blink: true,
    },
    events: ["1.00 calib_dot_show", "2.00 calib_dot_click"],
    gate: {
      attempt: 2,
      passed: false,
      meanErrorNorm: 0.1234,
      pointErrors: [0.05, 0.2],
      validFramesPerDot: [12, 3],
      reason: "head moved",
    },
    gateErrors: null,
    quality: { ...defaults },
    ...overrides,
  };
}

const drawCalls: string[] = [];
const fakeCtx = new Proxy(
  {},
  {
    get: (_t, name: string) => (): void => {
      drawCalls.push(name);
    },
    set: () => true,
  },
);

let root: HTMLElement;
const onQualityChange = vi.fn();

beforeEach(() => {
  document.body.innerHTML = '<main id="app"></main>';
  root = document.getElementById("app") as HTMLElement;
  drawCalls.length = 0;
  onQualityChange.mockReset();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => fakeCtx as never);
});
afterEach(() => {
  vi.restoreAllMocks();
});

function make(): ReturnType<typeof createDebugOverlay> {
  return createDebugOverlay(root, { onQualityChange }, { qualityDefaults: defaults });
}
const text = (name: string): string =>
  root.querySelector(`[data-debug="${name}"]`)?.textContent ?? "";
const slider = (key: string): HTMLInputElement =>
  root.querySelector(`input[data-key="${key}"]`) as HTMLInputElement;
function drag(key: string, value: number): void {
  const input = slider(key);
  input.value = String(value);
  input.dispatchEvent(new Event("input"));
}

describe("formatBytes", () => {
  it("uses B, KB, MB and GB", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1023)).toBe("1023 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
    expect(formatBytes(3 * 1024 ** 3)).toBe("3.0 GB");
  });
});

describe("createDebugOverlay", () => {
  it("is clearly marked DEBUG and shows nothing until it is created", () => {
    expect(root.children).toHaveLength(0);
    make();
    expect(root.querySelector(".debug-panel h2")?.textContent).toBe("DEBUG");
  });

  it("renders the counters, signals, gate numbers and events it is given", () => {
    const overlay = make();
    overlay.update(view());
    const counters = text("counters");
    expect(counters).toContain("29.8");
    expect(counters).toContain("14.2");
    expect(counters).toContain("23.5 ms");
    expect(counters).toContain("7");
    expect(counters).toContain("3 chunks, 1.5 MB");
    const signals = text("signals");
    expect(signals).toContain("0.47");
    expect(signals).toContain("0.153");
    expect(signals).toContain("-3.5");
    expect(signals).toContain("1.3");
    expect(signals).toMatch(/blink\s+yes/);
    const gate = text("gate");
    expect(gate).toContain("attempt 2: FAILED");
    expect(gate).toContain("0.123");
    expect(gate).toContain("0.050 0.200");
    expect(gate).toContain("12 3");
    expect(gate).toContain("head moved");
    expect(text("events")).toBe("1.00 calib_dot_show\n2.00 calib_dot_click");
  });

  it("shows a dash for missing numbers and says when there is no gate result", () => {
    const overlay = make();
    const v = view({ gate: null });
    overlay.update({
      ...v,
      counters: {
        webcamFps: null,
        faceModelFps: null,
        inferenceMs: null,
        droppedStimulusFrames: null,
        uploadQueueChunks: null,
        uploadPendingBytes: null,
      },
    });
    expect(text("counters")).toContain("–");
    expect(text("gate")).toBe("no gate result yet");
  });

  it("shows the measured value next to each threshold slider", () => {
    const overlay = make();
    overlay.update(view());
    const rows = [...root.querySelectorAll(".debug-slider-text")].map((e) => e.textContent);
    expect(rows).toHaveLength(8);
    expect(rows.find((r) => r?.startsWith("brightness min"))).toContain("now 0.47");
    expect(rows.find((r) => r?.startsWith("max yaw"))).toContain("now 3.50");
    expect(slider("brightness_min").value).toBe("0.25");
    expect(slider("max_yaw_deg").value).toBe("25");
  });

  it("mounts the webcam element in the preview and draws the face and gate canvases", () => {
    const overlay = make();
    const video = document.createElement("video");
    overlay.update(
      view({
        webcam: video,
        face: { box: null, landmarks: [{ x: 0.5, y: 0.5 }], irisCenters: [] },
        gateErrors: [{ target: { x: 0.5, y: 0.5 }, prediction: { x: 0.4, y: 0.4 } }],
      }),
    );
    expect(root.querySelector(".debug-preview")?.contains(video)).toBe(true);
    expect(drawCalls).toContain("fillRect");
    expect(drawCalls).toContain("lineTo");
    overlay.update(view({ webcam: null }));
    expect(video.isConnected).toBe(false);
  });

  it("canvases do not take pointer events, the panel does, and the overlay never covers the page", () => {
    make();
    const gate = root.querySelector<HTMLElement>(".debug-gate-canvas");
    const face = root.querySelector<HTMLElement>(".debug-preview canvas");
    expect(gate?.parentElement?.className).toBe("debug-root");
    expect(face?.style.pointerEvents).toBe("none");
    // The gate canvas is fixed and click-through by the stylesheet.
    expect(root.querySelector("style")?.textContent).toMatch(
      /\.debug-gate-canvas \{[^}]*pointer-events: none/,
    );
  });

  describe("threshold sliders", () => {
    it("calls onQualityChange with the full new thresholds when valid", () => {
      const overlay = make();
      overlay.update(view());
      drag("brightness_min", 0.3);
      expect(onQualityChange).toHaveBeenCalledTimes(1);
      expect(onQualityChange).toHaveBeenCalledWith({ ...defaults, brightness_min: 0.3 });
    });

    it("does not call it when a lower bound would pass its upper bound, and marks the slider", () => {
      const overlay = make();
      overlay.update(view());
      drag("brightness_min", 0.9);
      expect(onQualityChange).not.toHaveBeenCalled();
      expect(slider("brightness_min").parentElement?.classList.contains("invalid")).toBe(true);
      drag("brightness_min", 0.35);
      expect(onQualityChange).toHaveBeenCalledTimes(1);
      expect(slider("brightness_min").parentElement?.classList.contains("invalid")).toBe(false);
    });

    it("builds on earlier valid changes", () => {
      const overlay = make();
      overlay.update(view());
      drag("brightness_min", 0.3);
      drag("max_yaw_deg", 30);
      expect(onQualityChange).toHaveBeenLastCalledWith({
        ...defaults,
        brightness_min: 0.3,
        max_yaw_deg: 30,
      });
    });
  });

  describe("Copy as JSON", () => {
    const click = (): void =>
      (root.querySelector(".debug-panel button") as HTMLButtonElement).click();
    const flush = async (): Promise<void> => {
      for (let i = 0; i < 5; i++) await Promise.resolve();
    };

    afterEach(() => {
      Reflect.deleteProperty(navigator, "clipboard");
    });

    it("writes only the changed keys to the clipboard", async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
      const overlay = make();
      overlay.update(view());
      drag("brightness_min", 0.3);
      click();
      await flush();
      expect(writeText).toHaveBeenCalledWith('"quality": {\n  "brightness_min": 0.3\n}');
      expect(root.querySelector(".debug-copy-status")?.textContent).toBe("Copied.");
      expect(root.querySelector<HTMLTextAreaElement>("textarea")?.hidden).toBe(true);
    });

    it("shows the JSON in a text area when the clipboard fails", async () => {
      const writeText = vi.fn().mockRejectedValue(new Error("denied"));
      Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
      const overlay = make();
      overlay.update(view());
      click();
      await flush();
      const area = root.querySelector<HTMLTextAreaElement>("textarea");
      expect(area?.hidden).toBe(false);
      expect(area?.value).toBe('"quality": {}');
    });

    it("shows the text area when there is no clipboard API", () => {
      Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
      const overlay = make();
      overlay.update(view());
      drag("max_pitch_deg", 15);
      click();
      expect(root.querySelector<HTMLTextAreaElement>("textarea")?.value).toBe(
        '"quality": {\n  "max_pitch_deg": 15\n}',
      );
    });
  });

  it("setVisible hides and shows the whole overlay", () => {
    const overlay = make();
    const wrapper = root.querySelector<HTMLElement>(".debug-root");
    overlay.setVisible(false);
    expect(wrapper?.hidden).toBe(true);
    overlay.setVisible(true);
    expect(wrapper?.hidden).toBe(false);
  });

  it("destroy removes everything, including the webcam element it holds", () => {
    const overlay = make();
    const video = document.createElement("video");
    overlay.update(view({ webcam: video }));
    overlay.destroy();
    expect(root.children).toHaveLength(0);
    expect(video.isConnected).toBe(false);
    expect(document.querySelector(".debug-panel")).toBeNull();
  });
});
