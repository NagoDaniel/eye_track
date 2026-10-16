// The running session: event log, features and stimulus frames, each through a JSONL uploader into
// the upload queue (PLAN §5.3, §5.4, §5.8, §5.9).

import type { Api } from "./api";
import { clockOrigin } from "./clock";
import type { SessionCreateResponse } from "./config";
import { EventLog, type EventDataMap, type EventRecord, type EventSink } from "./eventLog";
import type { FeatureRecord, StimulusFrameRecord } from "./records";
import { EventsUploader } from "./upload/eventsUploader";
import { JsonlUploader } from "./upload/jsonlUploader";
import type { UploadDb } from "./upload/idb";
import { installUnloadGuard } from "./upload/unloadGuard";
import { UploadQueue } from "./upload/uploadQueue";

export interface SessionRuntime {
  log: EventLog;
  events: EventsUploader;
  features: JsonlUploader<FeatureRecord>;
  stimulusFrames: JsonlUploader<StimulusFrameRecord>;
  queue: UploadQueue;
  /** Extra listeners of every logged event (debug panel, calibration gate); called after the upload sink. */
  eventSinks: Set<EventSink>;
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
  const chunkMs = session.config.recording.chunk_ms;
  const events = new EventsUploader(queue, chunkMs, onFatal);
  const features = new JsonlUploader<FeatureRecord>(queue, "features", chunkMs, onFatal);
  const stimulusFrames = new JsonlUploader<StimulusFrameRecord>(
    queue,
    "stimulus_frames",
    chunkMs,
    onFatal,
  );
  const eventSinks = new Set<EventSink>();
  const log = new EventLog((record: EventRecord) => {
    events.push(record);
    for (const sink of eventSinks) sink(record);
  });
  const releaseUnloadGuard = installUnloadGuard(() => true);
  events.start();
  features.start();
  stimulusFrames.start();
  log.log("session_start", { client_info: clientInfo() });
  return { log, events, features, stimulusFrames, queue, eventSinks, releaseUnloadGuard };
}
