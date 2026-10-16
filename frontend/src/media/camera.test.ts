import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CAMERA_MESSAGES,
  CameraError,
  MIN_CAMERA_HEIGHT,
  MIN_CAMERA_WIDTH,
  requestCamera,
  type CameraErrorKind,
} from "./camera";

const RECORDING = { width: 1280, height: 720, fps: 30 };

class FakeTrack extends EventTarget {
  stopped = 0;
  constructor(private readonly settings: MediaTrackSettings) {
    super();
  }
  getSettings(): MediaTrackSettings {
    return this.settings;
  }
  stop(): void {
    this.stopped += 1;
  }
  /** Camera unplugged: the browser fires `ended` on its own. */
  unplug(): void {
    this.dispatchEvent(new Event("ended"));
  }
}

function fakeStream(track: FakeTrack): MediaStream {
  return {
    getTracks: () => [track],
    getVideoTracks: () => [track],
  } as unknown as MediaStream;
}

function installGetUserMedia(impl: (c: MediaStreamConstraints) => Promise<MediaStream>) {
  const getUserMedia = vi.fn(impl);
  Object.defineProperty(navigator, "mediaDevices", {
    value: { getUserMedia },
    configurable: true,
  });
  return getUserMedia;
}

afterEach(() => {
  Reflect.deleteProperty(navigator, "mediaDevices");
});

describe("requestCamera", () => {
  it("asks for the ideal size and frame rate from the study config, without audio", async () => {
    const track = new FakeTrack({ width: 1280, height: 720, frameRate: 30 });
    const gum = installGetUserMedia(() => Promise.resolve(fakeStream(track)));
    await requestCamera(RECORDING);
    expect(gum).toHaveBeenCalledWith({
      video: {
        width: { ideal: 1280 },
        height: { ideal: 720 },
        frameRate: { ideal: 30 },
        facingMode: "user",
      },
      audio: false,
    });
  });

  it("records the actual track settings", async () => {
    const settings = { width: 960, height: 540, frameRate: 24, deviceId: "cam-1" };
    const track = new FakeTrack(settings);
    installGetUserMedia(() => Promise.resolve(fakeStream(track)));
    const camera = await requestCamera(RECORDING);
    expect(camera.settings).toEqual(settings);
    expect(camera.track).toBe(track);
  });

  it("accepts exactly the minimum size", async () => {
    const track = new FakeTrack({ width: MIN_CAMERA_WIDTH, height: MIN_CAMERA_HEIGHT });
    installGetUserMedia(() => Promise.resolve(fakeStream(track)));
    await expect(requestCamera(RECORDING)).resolves.toBeDefined();
    expect(track.stopped).toBe(0);
  });

  it("rejects a 320x240 stream as too_small and stops its tracks", async () => {
    const track = new FakeTrack({ width: 320, height: 240 });
    installGetUserMedia(() => Promise.resolve(fakeStream(track)));
    const error = await requestCamera(RECORDING).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(CameraError);
    expect((error as CameraError).kind).toBe("too_small");
    expect((error as CameraError).message).toBe(CAMERA_MESSAGES.too_small);
    expect(track.stopped).toBe(1);
  });

  it("rejects a stream whose settings have no resolution, and stops it", async () => {
    const track = new FakeTrack({});
    installGetUserMedia(() => Promise.resolve(fakeStream(track)));
    await expect(requestCamera(RECORDING)).rejects.toMatchObject({ kind: "other" });
    expect(track.stopped).toBe(1);
  });

  it("reports unsupported when there is no mediaDevices", async () => {
    await expect(requestCamera(RECORDING)).rejects.toMatchObject({ kind: "unsupported" });
  });

  const cases: [string, CameraErrorKind][] = [
    ["NotAllowedError", "denied"],
    ["SecurityError", "denied"],
    ["NotFoundError", "not_found"],
    ["OverconstrainedError", "not_found"],
    ["NotReadableError", "in_use"],
    ["AbortError", "in_use"],
    ["SomethingElseError", "other"],
  ];
  it.each(cases)("maps %s to %s with a participant-friendly message", async (name, kind) => {
    installGetUserMedia(() => Promise.reject(new DOMException("technical text", name)));
    const error = await requestCamera(RECORDING).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(CameraError);
    expect((error as CameraError).kind).toBe(kind);
    expect((error as CameraError).message).toBe(CAMERA_MESSAGES[kind]);
    expect((error as CameraError).message).not.toContain("technical text");
    expect((error as CameraError).detail).toContain(name);
  });

  it("maps a non-DOMException failure (e.g. TypeError) to other", async () => {
    installGetUserMedia(() => Promise.reject(new TypeError("bad constraints")));
    await expect(requestCamera(RECORDING)).rejects.toMatchObject({ kind: "other" });
  });

  it("onEnded fires when the track ends, and not after unsubscribing", async () => {
    const track = new FakeTrack({ width: 1280, height: 720 });
    installGetUserMedia(() => Promise.resolve(fakeStream(track)));
    const camera = await requestCamera(RECORDING);
    const first = vi.fn();
    const second = vi.fn();
    camera.onEnded(first);
    const off = camera.onEnded(second);
    off();
    track.unplug();
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).not.toHaveBeenCalled();
  });

  it("stop() stops the tracks and is idempotent", async () => {
    const track = new FakeTrack({ width: 1280, height: 720 });
    installGetUserMedia(() => Promise.resolve(fakeStream(track)));
    const camera = await requestCamera(RECORDING);
    camera.stop();
    expect(track.stopped).toBe(1);
    camera.stop();
    expect(track.stopped).toBe(1);
  });
});
