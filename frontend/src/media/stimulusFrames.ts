// Stimulus frame logging (PLAN §5.3): one record per frame the stimulus <video> presents, the
// authoritative mapping from the client clock (`t`) to video position (`media_time`).
// Wiring into the app flow and the upload stream is done by the caller.

import { now } from "../clock";
import type { StimulusFrameRecord } from "../records";

/**
 * Element events after which `presentedFrames` is not comparable with the previous frame. Browsers
 * do not guarantee callbacks while a video is paused, and the counter can jump over a pause, a seek
 * or a stall; a gap there is not a dropped frame.
 */
export const RUN_BREAK_EVENTS: readonly string[] = ["pause", "seeking", "waiting", "ended"];

/**
 * Video frames between two consecutive callbacks that were never seen: the media-time step divided
 * by the frame duration, minus the one frame that was seen. `previousMediaTime` is null for the first
 * frame of a run, which has no predecessor, so it counts 0. A run starts with a new
 * `startStimulusFrameLogging` (one per video element, so the first frame of a second video starts a
 * new run) and after every event in RUN_BREAK_EVENTS.
 *
 * The media time is used, not `presentedFrames`: on a real Mac (Chrome, 2x display) that counter rose
 * by 2 for every frame of a 30 fps video, which made every frame look dropped.
 */
export function droppedFrames(
  previousMediaTime: number | null,
  mediaTime: number,
  fps: number,
): number {
  if (previousMediaTime === null) return 0;
  return Math.max(0, Math.round((mediaTime - previousMediaTime) * fps) - 1);
}

/**
 * Calls `onFrame` for every presented frame of `video` with the record to log and the number of
 * frames dropped since the previous frame of the same run. Returns an idempotent `stop()`.
 * Throws when the browser lacks `requestVideoFrameCallback` (the app requires it, PLAN §5.1).
 */
export function startStimulusFrameLogging(
  video: HTMLVideoElement,
  onFrame: (record: StimulusFrameRecord, droppedSincePrevious: number) => void,
  /** Frames per second of the video, to turn a media-time step into a number of frames. */
  fps: number,
): () => void {
  if (!(fps > 0)) throw new Error("the video's frame rate must be positive");
  if (typeof video.requestVideoFrameCallback !== "function") {
    throw new Error("requestVideoFrameCallback is not supported: stimulus frames cannot be logged");
  }

  let previousMediaTime: number | null = null;
  let handle: number | null = null;
  let stopped = false;

  const onVideoFrame: VideoFrameRequestCallback = (_domNow, metadata) => {
    handle = null;
    if (stopped) return;
    const record: StimulusFrameRecord = {
      t: now(),
      expected_display_time: metadata.expectedDisplayTime,
      media_time: metadata.mediaTime,
      presented_frames: metadata.presentedFrames,
    };
    const dropped = droppedFrames(previousMediaTime, metadata.mediaTime, fps);
    previousMediaTime = metadata.mediaTime;
    // Re-arm before calling out, so a throwing consumer cannot end the logging.
    handle = video.requestVideoFrameCallback(onVideoFrame);
    onFrame(record, dropped);
  };

  const breakRun = (): void => {
    previousMediaTime = null;
  };

  for (const type of RUN_BREAK_EVENTS) video.addEventListener(type, breakRun);
  handle = video.requestVideoFrameCallback(onVideoFrame);

  return () => {
    if (stopped) return;
    stopped = true;
    for (const type of RUN_BREAK_EVENTS) video.removeEventListener(type, breakRun);
    if (handle !== null) video.cancelVideoFrameCallback(handle);
    handle = null;
  };
}
