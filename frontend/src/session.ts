// The running session: event log -> events uploader -> upload queue (PLAN §5.3, §5.8, §5.9).

import type { Api } from "./api";
import { clockOrigin } from "./clock";
import type { SessionCreateResponse } from "./config";
import { EventLog, type EventDataMap } from "./eventLog";
import { EventsUploader } from "./upload/eventsUploader";
import type { UploadDb } from "./upload/idb";
import { installUnloadGuard } from "./upload/unloadGuard";
import { UploadQueue } from "./upload/uploadQueue";

export interface SessionRuntime {
  log: EventLog;
  events: EventsUploader;
  queue: UploadQueue;
  /** Stop warning on unload; call once the upload is complete. */
  releaseUnloadGuard: () => void;
}

export function clientInfo(): EventDataMap["session_start"]["client_info"] {
  const origin = clockOrigin();
  return {
    user_agent: navigator.userAgent,
    platform: navigator.platform,
    hardware_concurrency: navigator.hardwareConcurrency,
    screen: { width: screen.width, height: screen.height },
    device_pixel_ratio: devicePixelRatio,
    time_origin: origin.time_origin,
    wall_clock_ms: origin.wall_clock_ms,
  };
}

/** Called after consent: opens the queue, starts the events chunking and logs `session_start`. */
export async function startRuntime(options: {
  db: UploadDb;
  api: Api;
  session: SessionCreateResponse;
  studyCode: string;
  /** A loud, unrecoverable upload failure (409, 401, ...). */
  onFatal: (error: Error) => void;
}): Promise<SessionRuntime> {
  const { db, api, session, studyCode, onFatal } = options;
  const queue = await UploadQueue.open(
    db,
    api,
    { sessionId: session.session_id, uploadToken: session.upload_token, studyCode, nextSeq: {} },
    { onError: onFatal },
  );
  const events = new EventsUploader(queue, session.config.recording.chunk_ms, onFatal);
  const log = new EventLog(events.push);
  const releaseUnloadGuard = installUnloadGuard(() => true);
  events.start();
  log.log("session_start", { client_info: clientInfo() });
  return { log, events, queue, releaseUnloadGuard };
}
