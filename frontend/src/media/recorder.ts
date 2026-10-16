// Webcam MediaRecorder (PLAN §5.3, §5.4): one continuous WebM byte stream, one chunk per timeslice,
// chunks handed to the upload queue in order.

import type { EventLog } from "../eventLog";

/** Mime type preference order (PLAN §5.4). */
export const RECORDER_MIME_TYPES = [
  "video/webm;codecs=vp9",
  "video/webm;codecs=vp8",
  "video/webm",
] as const;

/** Video bitrate, about 2.5 Mbps (PLAN §5.4). */
export const RECORDER_BITS_PER_SECOND = 2_500_000;

export function pickMimeType(isTypeSupported: (mimeType: string) => boolean): string {
  const found = RECORDER_MIME_TYPES.find((type) => isTypeSupported(type));
  if (found === undefined) {
    throw new Error(`MediaRecorder supports none of: ${RECORDER_MIME_TYPES.join(", ")}`);
  }
  return found;
}

export interface RecorderOptions {
  stream: MediaStream;
  /** Camera `track.getSettings()`, written into the `recorder_start` event. */
  settings: Record<string, unknown>;
  log: EventLog;
  /** Stores one chunk and resolves with its seq. Called strictly in recording order. */
  enqueue: (bytes: Uint8Array) => Promise<number>;
  /** `chunk_ms` from the study config (2000 ms). */
  timesliceMs: number;
  onError: (error: Error) => void;
  bitsPerSecond?: number;
}

export interface RecorderTotals {
  chunks: number;
  bytes: number;
}

export interface RecorderHandle {
  /** Stops recording; resolves after the final chunk was enqueued and `recorder_stop` logged. */
  stop(): Promise<RecorderTotals>;
  mimeType: string;
}

function toError(value: unknown): Error {
  if (value instanceof Error) return value;
  // MediaRecorder `error` events carry the failure in `event.error` (a DOMException).
  const inner = (value as { error?: unknown } | null)?.error;
  if (inner instanceof Error) return inner;
  return new Error(`MediaRecorder error: ${String(inner ?? value)}`);
}

export function startRecorder(options: RecorderOptions): RecorderHandle {
  const { stream, settings, log, enqueue, timesliceMs, onError } = options;
  const bitsPerSecond = options.bitsPerSecond ?? RECORDER_BITS_PER_SECOND;
  const mimeType = pickMimeType((type) => MediaRecorder.isTypeSupported(type));
  const recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: bitsPerSecond });

  let chunks = 0;
  let bytes = 0;
  let failure: Error | null = null;
  // Blob reads and enqueues run one after the other, so chunk order equals recording order even
  // when arrayBuffer() calls resolve out of order.
  let chain: Promise<void> = Promise.resolve();

  let finish!: { resolve: (t: RecorderTotals) => void; reject: (e: Error) => void };
  const done = new Promise<RecorderTotals>((resolve, reject) => {
    finish = { resolve, reject };
  });
  done.catch(() => undefined); // observed through stop(); avoids an unhandled rejection if never stopped

  const fail = (error: Error): void => {
    if (failure !== null) return;
    failure = error;
    onError(error);
    finish.reject(error);
  };

  recorder.addEventListener("start", () => {
    log.log("recorder_start", { mime_type: mimeType, bits_per_second: bitsPerSecond, settings });
  });

  recorder.addEventListener("dataavailable", (event) => {
    const blob = (event as BlobEvent).data;
    if (blob.size === 0) return;
    chain = chain.then(async () => {
      if (failure !== null) return;
      try {
        const data = new Uint8Array(await blob.arrayBuffer());
        const seq = await enqueue(data);
        chunks += 1;
        bytes += data.byteLength;
        log.log("recorder_dataavailable", { seq, size: data.byteLength });
      } catch (error) {
        fail(toError(error));
      }
    });
  });

  recorder.addEventListener("error", (event) => {
    fail(toError(event));
  });

  recorder.addEventListener("stop", () => {
    // The final dataavailable fires before stop, so by the time the chain drains it is enqueued.
    void chain.then(() => {
      if (failure !== null) return;
      const totals = { chunks, bytes };
      log.log("recorder_stop", totals);
      finish.resolve(totals);
    });
  });

  recorder.start(timesliceMs);

  return {
    mimeType,
    stop(): Promise<RecorderTotals> {
      if (recorder.state !== "inactive") recorder.stop();
      return done;
    },
  };
}
