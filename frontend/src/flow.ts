// The screen state machine (PLAN §5.2): runs the screens in order. Any failure ends on the error screen.

import { Api } from "./api";
import { assertPointCounts } from "./calibration/points";
import type { SessionCreateResponse } from "./config";
import { createErrorReporter } from "./errorReporter";
import type { EventLog } from "./eventLog";
import { parseRoute } from "./route";
import { calibrationScreen } from "./screens/calibration";
import { consentScreen } from "./screens/consent";
import { deviceCheckScreen } from "./screens/deviceCheck";
import { doneScreen } from "./screens/done";
import { browserGate, entryScreen } from "./screens/entry";
import { errorScreen } from "./screens/error";
import { fullscreenScreen } from "./screens/fullscreen";
import { instructionsScreen } from "./screens/instructions";
import { playbackScreen } from "./screens/playback";
import { preloadScreen } from "./screens/preload";
import { resumeScreen } from "./screens/resume";
import { resumeUnfinished } from "./screens/resumeFlow";
import { uploadScreen } from "./screens/upload";
import { validationScreen } from "./screens/validation";
import { startRuntime } from "./session";
import { SessionEvents } from "./sessionEvents";
import { sessionPlan } from "./sessionPlan";
import { UploadDb } from "./upload/idb";
import { installUnloadGuard } from "./upload/unloadGuard";
import { UploadQueue } from "./upload/uploadQueue";

interface Shared {
  sessionId: string | null;
  log: EventLog | null;
}

export async function runApp(root: HTMLElement, api: Api): Promise<never> {
  const shared: Shared = { sessionId: null, log: null };
  const reporter = createErrorReporter({
    api,
    getLog: () => shared.log,
    getSessionId: () => shared.sessionId,
  });
  reporter.install(window);
  try {
    return await run(root, api, shared);
  } catch (e) {
    reporter.report(e);
    return errorScreen(root, e, shared.sessionId);
  }
}

async function run(root: HTMLElement, api: Api, shared: Shared): Promise<never> {
  const route = parseRoute(location.pathname, location.search);
  const study = await api.getStudy(route.studyCode);
  await browserGate(root, study);
  const db = await UploadDb.open();

  // Sessions left unfinished by a reload: upload what is left and end each as interrupted (PLAN §5.8).
  const uploaded = await resumeUnfinished(await db.listSessions(), {
    pendingChunks: async (s) => (await db.chunksOf(s.sessionId)).length,
    ask: (s, pending, problem) => resumeScreen(root, s, pending, problem),
    resume: async (s) => {
      shared.sessionId = s.sessionId;
      const release = installUnloadGuard(() => true);
      try {
        const queue = await UploadQueue.open(db, api, s);
        await uploadScreen(root, queue, { events: null, interrupted: true });
      } finally {
        release(); // also after a failure, or the page would warn on close forever
      }
    },
    discard: (s) => db.deleteSession(s.sessionId),
  });
  if (uploaded > 0) return doneScreen(root);

  const session = await entryScreen(root, study, route.participantCode, (participantCode) =>
    api.createSession({ study_code: study.code, participant_code: participantCode }),
  );
  shared.sessionId = session.session_id;
  assertPointCounts(session.config); // fail loudly before the participant has done anything

  // A failure of the upload queue ends the session on the error screen, whatever screen is showing.
  let fail: (error: Error) => void = () => undefined;
  const fatal = new Promise<never>((_, reject) => {
    fail = reject;
  });
  fatal.catch(() => undefined); // it is observed by the races below
  const step = <T>(screen: Promise<T>): Promise<T> => Promise.race([screen, fatal]);

  await step(
    consentScreen(root, study, () =>
      api.recordConsent(session.session_id, session.upload_token, {
        consent_version: session.config.consent_version,
      }),
    ),
  );
  const runtime = await startRuntime({ db, api, session, studyCode: study.code, onFatal: fail });
  shared.log = runtime.log;
  const events = new SessionEvents(runtime.log);
  events.start();

  const videoUrls = await step(preloadScreen(root, session.videos));
  await step(deviceCheckScreen(root));
  await step(instructionsScreen(root));

  for (const planned of sessionPlan(session.config)) {
    switch (planned.kind) {
      case "fullscreen":
        await step(fullscreenScreen(root, session.config.playback.require_fullscreen));
        break;
      case "calibration":
        await step(calibrationScreen(root, session.config, runtime.log));
        break;
      case "validation":
        await step(validationScreen(root, session.config, runtime.log));
        break;
      case "playback": {
        const { video, url } = findVideo(session, videoUrls, planned.videoId);
        await step(playbackScreen(root, video, url, session.config.playback, runtime.log, events));
        break;
      }
      case "upload":
        await step(uploadScreen(root, runtime.queue, { events: runtime.events, interrupted: false }));
        events.stop();
        runtime.releaseUnloadGuard();
        break;
      case "done":
        return doneScreen(root);
    }
  }
  throw new Error("The session plan has no final step");
}

function findVideo(
  session: SessionCreateResponse,
  urls: Map<string, string>,
  videoId: string,
): { video: SessionCreateResponse["videos"][number]; url: string } {
  const video = session.videos.find((v) => v.id === videoId);
  const url = urls.get(videoId);
  if (video === undefined || url === undefined) {
    throw new Error(`The playlist names video ${videoId}, which the server did not provide`);
  }
  return { video, url };
}
