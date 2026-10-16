import { afterEach, describe, expect, it, vi } from "vitest";

import type { StimulusFrameRecord } from "../records";
import { droppedFrames, startStimulusFrameLogging } from "./stimulusFrames";

interface FrameMetadata {
  expectedDisplayTime: number;
  mediaTime: number;
  presentedFrames: number;
}

/** A video element with a controllable requestVideoFrameCallback. */
class FakeVideo extends EventTarget {
  private nextHandle = 1;
  private pending = new Map<number, VideoFrameRequestCallback>();
  readonly cancelled: number[] = [];

  requestVideoFrameCallback(callback: VideoFrameRequestCallback): number {
    const handle = this.nextHandle++;
    this.pending.set(handle, callback);
    return handle;
  }

  cancelVideoFrameCallback(handle: number): void {
    this.cancelled.push(handle);
    this.pending.delete(handle);
  }

  queued(): VideoFrameRequestCallback[] {
    return [...this.pending.values()];
  }

  get pendingCount(): number {
    return this.pending.size;
  }

  /** Presents one frame: runs and clears the pending callbacks, as the browser does. */
  present(metadata: FrameMetadata): void {
    const callbacks = [...this.pending.values()];
    this.pending.clear();
    const full = { width: 1280, height: 720, ...metadata } as VideoFrameCallbackMetadata;
    for (const callback of callbacks) callback(0, full);
  }
}

function frame(presentedFrames: number): FrameMetadata {
  return {
    expectedDisplayTime: 1000 + presentedFrames * 40,
    mediaTime: presentedFrames / 25,
    presentedFrames,
  };
}

interface Logged {
  record: StimulusFrameRecord;
  dropped: number;
}

function setup(): { video: FakeVideo; frames: Logged[]; stop: () => void } {
  const video = new FakeVideo();
  const frames: Logged[] = [];
  const stop = startStimulusFrameLogging(
    video as unknown as HTMLVideoElement,
    (record, dropped) => {
      frames.push({ record, dropped });
    },
  );
  return { video, frames, stop };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("droppedFrames", () => {
  it("is 0 without a previous frame", () => {
    expect(droppedFrames(null, 500)).toBe(0);
  });

  it("is 0 for consecutive frames and counts the gap otherwise", () => {
    expect(droppedFrames(10, 11)).toBe(0);
    expect(droppedFrames(10, 12)).toBe(1);
    expect(droppedFrames(10, 15)).toBe(4);
  });

  it("never goes negative", () => {
    expect(droppedFrames(10, 10)).toBe(0);
    expect(droppedFrames(10, 3)).toBe(0);
  });
});

describe("startStimulusFrameLogging", () => {
  it("logs one record per presented frame with the clock time and the frame metadata", () => {
    vi.spyOn(performance, "now").mockReturnValueOnce(100.5).mockReturnValueOnce(140.25);
    const { video, frames } = setup();

    video.present(frame(1));
    video.present({ expectedDisplayTime: 2000.5, mediaTime: 0.08, presentedFrames: 2 });

    expect(frames).toEqual([
      {
        record: { t: 100.5, expected_display_time: 1040, media_time: 0.04, presented_frames: 1 },
        dropped: 0,
      },
      {
        record: { t: 140.25, expected_display_time: 2000.5, media_time: 0.08, presented_frames: 2 },
        dropped: 0,
      },
    ]);
    expect(video.pendingCount).toBe(1);
  });

  it("reports dropped frames from gaps in presentedFrames", () => {
    const { video, frames } = setup();
    for (const n of [1, 2, 5, 6]) video.present(frame(n));
    expect(frames.map((f) => f.dropped)).toEqual([0, 0, 2, 0]);
  });

  it.each(["pause", "seeking", "waiting", "ended"])(
    "does not count a jump across a %s as dropped frames",
    (type) => {
      const { video, frames } = setup();
      video.present(frame(10));
      video.dispatchEvent(new Event(type));
      video.present(frame(80));
      video.present(frame(81));
      expect(frames.map((f) => f.dropped)).toEqual([0, 0, 0]);
    },
  );

  it("counts drops again after a run restarted", () => {
    const { video, frames } = setup();
    video.present(frame(10));
    video.dispatchEvent(new Event("pause"));
    video.present(frame(80));
    video.present(frame(83));
    expect(frames.map((f) => f.dropped)).toEqual([0, 0, 2]);
  });

  it("starts a new run for a second video, with its own counter", () => {
    const first = setup();
    first.video.present(frame(100));
    first.stop();

    const second = setup();
    second.video.present(frame(3));
    second.video.present(frame(4));
    expect(second.frames.map((f) => f.dropped)).toEqual([0, 0]);
  });

  it("stop cancels the pending callback, ends logging and is idempotent", () => {
    const { video, frames, stop } = setup();
    video.present(frame(1));
    expect(video.pendingCount).toBe(1);

    stop();
    expect(video.pendingCount).toBe(0);
    expect(video.cancelled).toHaveLength(1);
    stop();
    expect(video.cancelled).toHaveLength(1);

    video.present(frame(2));
    expect(frames).toHaveLength(1);
  });

  it("stop also detaches the run-break listeners", () => {
    const { video, stop } = setup();
    const remove = vi.spyOn(video, "removeEventListener");
    stop();
    expect(remove.mock.calls.map((call) => call[0]).sort()).toEqual([
      "ended",
      "pause",
      "seeking",
      "waiting",
    ]);
  });

  it("ignores a callback that ran after stop", () => {
    const video = new FakeVideo();
    const frames: Logged[] = [];
    const stop = startStimulusFrameLogging(
      video as unknown as HTMLVideoElement,
      (record, dropped) => {
        frames.push({ record, dropped });
      },
    );
    // The browser already queued the callback when stop() is called, and ignores the cancel.
    const queued = video.queued();
    stop();
    for (const callback of queued) callback(0, frame(1) as unknown as VideoFrameCallbackMetadata);
    expect(frames).toHaveLength(0);
    expect(video.pendingCount).toBe(0);
  });

  it("keeps logging when the consumer throws on one frame", () => {
    const video = new FakeVideo();
    let calls = 0;
    startStimulusFrameLogging(video as unknown as HTMLVideoElement, () => {
      calls += 1;
      if (calls === 1) throw new Error("consumer failed");
    });
    expect(() => video.present(frame(1))).toThrow("consumer failed");
    expect(video.pendingCount).toBe(1);
    video.present(frame(2));
    expect(calls).toBe(2);
  });

  it("throws a clear error when requestVideoFrameCallback is missing", () => {
    const video = new EventTarget() as unknown as HTMLVideoElement;
    expect(() => startStimulusFrameLogging(video, () => undefined)).toThrow(
      /requestVideoFrameCallback/,
    );
  });
});
