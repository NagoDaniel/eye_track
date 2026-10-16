import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EventLog, type EventRecord } from "../eventLog";
import {
  pickMimeType,
  RECORDER_BITS_PER_SECOND,
  RECORDER_MIME_TYPES,
  startRecorder,
  type RecorderOptions,
} from "./recorder";

/** Hand-written MediaRecorder: the tests decide when events fire. */
class FakeMediaRecorder extends EventTarget {
  static supported: string[] = [...RECORDER_MIME_TYPES];
  static last: FakeMediaRecorder | null = null;
  static isTypeSupported(type: string): boolean {
    return FakeMediaRecorder.supported.includes(type);
  }

  state: "inactive" | "recording" = "inactive";
  timeslice: number | undefined;
  /** Blob delivered by the final dataavailable that `stop()` fires. */
  finalBlob: FakeBlob | null = null;

  constructor(
    readonly stream: MediaStream,
    readonly options: MediaRecorderOptions,
  ) {
    super();
    FakeMediaRecorder.last = this;
  }

  start(timeslice?: number): void {
    this.timeslice = timeslice;
    this.state = "recording";
    this.dispatchEvent(new Event("start"));
  }

  stop(): void {
    this.state = "inactive";
    this.dataAvailable(this.finalBlob ?? fakeBlob(new Uint8Array(0)));
    this.dispatchEvent(new Event("stop"));
  }

  dataAvailable(blob: FakeBlob): void {
    this.dispatchEvent(Object.assign(new Event("dataavailable"), { data: blob }));
  }

  fail(message: string): void {
    this.dispatchEvent(
      Object.assign(new Event("error"), { error: new DOMException(message, "EncodingError") }),
    );
  }
}

interface FakeBlob {
  size: number;
  arrayBuffer: () => Promise<ArrayBuffer>;
}

function fakeBlob(bytes: Uint8Array, delayMs = 0): FakeBlob {
  return {
    size: bytes.byteLength,
    arrayBuffer: () =>
      new Promise((resolve) => {
        setTimeout(() => {
          resolve(new Uint8Array(bytes).buffer);
        }, delayMs);
      }),
  };
}

const SETTINGS = { width: 1280, height: 720, frameRate: 30 };

function setup(overrides: Partial<RecorderOptions> = {}) {
  const events: EventRecord[] = [];
  const stored: Uint8Array[] = [];
  const onError = vi.fn();
  const enqueue = vi.fn((bytes: Uint8Array) => {
    stored.push(bytes);
    return Promise.resolve(stored.length - 1);
  });
  const handle = startRecorder({
    stream: {} as MediaStream,
    settings: SETTINGS,
    log: new EventLog((r) => events.push(r)),
    enqueue,
    timesliceMs: 2000,
    onError,
    ...overrides,
  });
  const recorder = FakeMediaRecorder.last as FakeMediaRecorder;
  return { handle, recorder, events, stored, onError, enqueue };
}

beforeEach(() => {
  FakeMediaRecorder.supported = [...RECORDER_MIME_TYPES];
  FakeMediaRecorder.last = null;
  vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("pickMimeType", () => {
  it("prefers vp9, then vp8, then plain webm", () => {
    expect(pickMimeType(() => true)).toBe("video/webm;codecs=vp9");
    expect(pickMimeType((t) => t !== "video/webm;codecs=vp9")).toBe("video/webm;codecs=vp8");
    expect(pickMimeType((t) => t === "video/webm")).toBe("video/webm");
  });

  it("throws a clear error when nothing is supported", () => {
    expect(() => pickMimeType(() => false)).toThrow(/supports none of/);
  });
});

describe("startRecorder", () => {
  it("uses the picked mime type, the default bitrate and the timeslice", () => {
    FakeMediaRecorder.supported = ["video/webm;codecs=vp8", "video/webm"];
    const { handle, recorder } = setup();
    expect(handle.mimeType).toBe("video/webm;codecs=vp8");
    expect(recorder.options).toEqual({
      mimeType: "video/webm;codecs=vp8",
      videoBitsPerSecond: RECORDER_BITS_PER_SECOND,
    });
    expect(RECORDER_BITS_PER_SECOND).toBe(2_500_000);
    expect(recorder.timeslice).toBe(2000);
    expect(recorder.state).toBe("recording");
  });

  it("takes a custom bitrate", () => {
    const { recorder, events } = setup({ bitsPerSecond: 1_000_000 });
    expect(recorder.options.videoBitsPerSecond).toBe(1_000_000);
    expect(events[0]?.data).toMatchObject({ bits_per_second: 1_000_000 });
  });

  it("throws when no mime type is supported, before touching MediaRecorder", () => {
    FakeMediaRecorder.supported = [];
    expect(() => setup()).toThrow(/supports none of/);
    expect(FakeMediaRecorder.last).toBeNull();
  });

  it("logs recorder_start from the start event with mime type, bitrate and camera settings", () => {
    const { events } = setup();
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      type: "recorder_start",
      data: {
        mime_type: "video/webm;codecs=vp9",
        bits_per_second: RECORDER_BITS_PER_SECOND,
        settings: SETTINGS,
      },
    });
  });

  it("enqueues chunks in recording order even when blob reads resolve out of order", async () => {
    const { handle, recorder, stored, events } = setup();
    recorder.dataAvailable(fakeBlob(new Uint8Array([1, 1, 1]), 30)); // slow
    recorder.dataAvailable(fakeBlob(new Uint8Array([2, 2]), 5));
    recorder.finalBlob = fakeBlob(new Uint8Array([3]), 0); // fastest
    const totals = await handle.stop();

    expect(stored.map((b) => Array.from(b))).toEqual([[1, 1, 1], [2, 2], [3]]);
    expect(totals).toEqual({ chunks: 3, bytes: 6 });
    const seqs = events.filter((e) => e.type === "recorder_dataavailable").map((e) => e.data);
    expect(seqs).toEqual([
      { seq: 0, size: 3 },
      { seq: 1, size: 2 },
      { seq: 2, size: 1 },
    ]);
  });

  it("waits for the previous enqueue before reading the next blob", async () => {
    const order: string[] = [];
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let n = 0;
    const enqueue = vi.fn(async (bytes: Uint8Array) => {
      order.push(`start${bytes[0]}`);
      if (n++ === 0) await gate;
      order.push(`end${bytes[0]}`);
      return n - 1;
    });
    const { handle, recorder } = setup({ enqueue });
    recorder.dataAvailable(fakeBlob(new Uint8Array([1])));
    recorder.finalBlob = fakeBlob(new Uint8Array([2]));
    const stopped = handle.stop();
    await new Promise((r) => setTimeout(r, 20));
    expect(order).toEqual(["start1"]);
    release();
    await stopped;
    expect(order).toEqual(["start1", "end1", "start2", "end2"]);
  });

  it("skips empty blobs", async () => {
    const { handle, recorder, enqueue, events } = setup();
    recorder.dataAvailable(fakeBlob(new Uint8Array(0)));
    recorder.dataAvailable(fakeBlob(new Uint8Array([9])));
    const totals = await handle.stop(); // the final blob is empty too
    expect(enqueue).toHaveBeenCalledTimes(1);
    expect(totals).toEqual({ chunks: 1, bytes: 1 });
    expect(events.filter((e) => e.type === "recorder_dataavailable")).toHaveLength(1);
  });

  it("logs recorder_start, then each recorder_dataavailable, then recorder_stop with the totals", async () => {
    const { handle, recorder, events } = setup();
    recorder.dataAvailable(fakeBlob(new Uint8Array(10)));
    recorder.dataAvailable(fakeBlob(new Uint8Array(20)));
    recorder.finalBlob = fakeBlob(new Uint8Array(5));
    await handle.stop();
    expect(events.map((e) => e.type)).toEqual([
      "recorder_start",
      "recorder_dataavailable",
      "recorder_dataavailable",
      "recorder_dataavailable",
      "recorder_stop",
    ]);
    expect(events.at(-1)?.data).toEqual({ chunks: 3, bytes: 35 });
    const ts = events.map((e) => e.t);
    expect(ts).toEqual([...ts].sort((a, b) => a - b));
  });

  it("stop() does not resolve before the final chunk was enqueued", async () => {
    const { handle, recorder, stored } = setup();
    recorder.finalBlob = fakeBlob(new Uint8Array([7, 7]), 40);
    let resolved = false;
    const stopped = handle.stop().then((t) => {
      resolved = true;
      return t;
    });
    await new Promise((r) => setTimeout(r, 10));
    expect(resolved).toBe(false);
    expect(stored).toHaveLength(0);
    expect(await stopped).toEqual({ chunks: 1, bytes: 2 });
    expect(stored).toHaveLength(1);
  });

  it("stop() twice returns the same totals and stops the recorder once", async () => {
    const { handle, recorder } = setup();
    const spy = vi.spyOn(recorder, "stop");
    recorder.finalBlob = fakeBlob(new Uint8Array([1]));
    const first = await handle.stop();
    const second = await handle.stop();
    expect(second).toEqual(first);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("calls onError and rejects a pending stop when the recorder fails", async () => {
    const { handle, recorder, onError, events } = setup();
    const stopped = handle.stop();
    const rejection = expect(stopped).rejects.toThrow("boom");
    recorder.fail("boom");
    await rejection;
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0]?.[0]).toBeInstanceOf(Error);
    expect(events.map((e) => e.type)).not.toContain("recorder_stop");
  });

  it("an error while recording reaches onError at once and makes a later stop() reject", async () => {
    const { handle, recorder, onError } = setup();
    recorder.fail("disk full");
    expect(onError).toHaveBeenCalledTimes(1);
    await expect(handle.stop()).rejects.toThrow("disk full");
  });

  it("an enqueue failure goes to onError, rejects stop and drops later chunks", async () => {
    const enqueue = vi
      .fn<(b: Uint8Array) => Promise<number>>()
      .mockRejectedValueOnce(new Error("idb full"))
      .mockResolvedValue(1);
    const { handle, recorder, onError, events } = setup({ enqueue });
    recorder.dataAvailable(fakeBlob(new Uint8Array([1])));
    recorder.finalBlob = fakeBlob(new Uint8Array([2]));
    await expect(handle.stop()).rejects.toThrow("idb full");
    expect(onError).toHaveBeenCalledTimes(1);
    expect(enqueue).toHaveBeenCalledTimes(1);
    expect(events.map((e) => e.type)).toEqual(["recorder_start"]);
  });
});
