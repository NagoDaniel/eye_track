// The screen state machine (PLAN §5.2): runs the screens in order. Any failure ends on the error screen.

import { Api } from "./api";
import { createErrorReporter } from "./errorReporter";
import type { EventLog } from "./eventLog";
import { parseRoute } from "./route";
import { startRuntime } from "./session";
import { browserGate, entryScreen } from "./screens/entry";
import { consentScreen } from "./screens/consent";
import { deviceCheckScreen } from "./screens/deviceCheck";
import { errorScreen } from "./screens/error";
import { fullscreenScreen } from "./screens/fullscreen";
import { instructionsScreen } from "./screens/instructions";
import { preloadScreen } from "./screens/preload";
import { UploadDb } from "./upload/idb";

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

  const session = await entryScreen(root, study, route.participantCode, (participantCode) =>
    api.createSession({ study_code: study.code, participant_code: participantCode }),
  );
  shared.sessionId = session.session_id;

  // A failure of the upload queue ends the session on the error screen, whatever screen is showing.
  let fail: (error: Error) => void = () => undefined;
  const fatal = new Promise<never>((_, reject) => {
    fail = reject;
  });
  fatal.catch(() => undefined); // handled by the races below
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

  await step(preloadScreen(root, session.videos));
  await step(deviceCheckScreen(root));
  await step(instructionsScreen(root));
  await step(fullscreenScreen(root, session.config.playback.require_fullscreen));
  throw new Error("The calibration and playback screens are not implemented yet");
}
