// Feature detection for the browser requirements a study lists (PLAN §5.1). Nothing here asks for a permission.

import type { BROWSER_REQUIREMENTS } from "./config";

type Requirement = (typeof BROWSER_REQUIREMENTS)[number];
export type FeatureChecks = Record<string, () => boolean>;

/** MediaRecorder with a WebM codec, in the preference order of PLAN §5.4. */
const WEBM_MIME_TYPES = ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"];

const CHECKS_FOR_PLAN_REQUIREMENTS: Record<Requirement, () => boolean> = {
  getUserMedia: () => typeof navigator !== "undefined" && typeof navigator.mediaDevices?.getUserMedia === "function",
  MediaRecorder: () =>
    typeof MediaRecorder !== "undefined" && WEBM_MIME_TYPES.some((type) => MediaRecorder.isTypeSupported(type)),
  requestVideoFrameCallback: () =>
    typeof HTMLVideoElement !== "undefined" && "requestVideoFrameCallback" in HTMLVideoElement.prototype,
  requestFullscreen: () => typeof document.documentElement.requestFullscreen === "function",
  IndexedDB: () => typeof indexedDB !== "undefined",
  WebAssembly: () => typeof WebAssembly === "object",
};

/**
 * APIs this app itself needs (events upload) that PLAN §5.1 does not list; checked in addition to the
 * study's list. See the 1b report: the list in shared/protocol.json has no entry for them.
 */
const CHECKS_FOR_APP: Record<string, () => boolean> = {
  CompressionStream: () => typeof CompressionStream !== "undefined",
  "crypto.subtle": () => typeof crypto !== "undefined" && crypto.subtle !== undefined,
};

export const FEATURE_CHECKS: FeatureChecks = { ...CHECKS_FOR_PLAN_REQUIREMENTS, ...CHECKS_FOR_APP };

const APP_REQUIREMENTS = Object.keys(CHECKS_FOR_APP);

/** Names from `required` (plus the app's own needs) that this browser lacks. An unknown name counts as missing. */
export function missingRequirements(
  required: readonly string[],
  checks: FeatureChecks = FEATURE_CHECKS,
  alsoRequired: readonly string[] = APP_REQUIREMENTS,
): string[] {
  const names = [...new Set([...required, ...alsoRequired])];
  return names.filter((name) => {
    const check = checks[name];
    return check === undefined || !check();
  });
}

export function blockingMessage(missing: readonly string[]): string {
  return (
    "This browser cannot run the study. Please use a current desktop version of Chrome or Edge. " +
    `Missing: ${missing.join(", ")}.`
  );
}
