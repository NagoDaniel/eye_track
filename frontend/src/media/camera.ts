// Webcam access (PLAN §5.2 step 4): ideal 1280x720 at 30 fps (from the study config), accept >= 640x480.

/** Smallest accepted camera stream (PLAN §5.2). A smaller stream is stopped and rejected. */
export const MIN_CAMERA_WIDTH = 640;
export const MIN_CAMERA_HEIGHT = 480;

export type CameraErrorKind =
  "denied" | "not_found" | "in_use" | "too_small" | "unsupported" | "other";

/** Participant-friendly messages: non-technical and say what to do next. */
export const CAMERA_MESSAGES: Record<CameraErrorKind, string> = {
  denied: "Camera access was blocked. Allow the camera in the address bar and reload the page.",
  not_found: "No camera was found. Connect a webcam and reload the page.",
  in_use:
    "The camera could not be started. Close other programs or tabs that use the camera, then reload the page.",
  too_small: `Your camera resolution is too low (at least ${MIN_CAMERA_WIDTH}x${MIN_CAMERA_HEIGHT} is needed). Try a different camera.`,
  unsupported:
    "This browser cannot use the camera. Please use a current version of Chrome or Edge.",
  other: "The camera could not be started. Reload the page and try again.",
};

export class CameraError extends Error {
  constructor(
    readonly kind: CameraErrorKind,
    /** Technical detail for the client error log; not for the participant. */
    readonly detail: string = "",
  ) {
    super(CAMERA_MESSAGES[kind]);
    this.name = "CameraError";
  }
}

export interface RecordingSettings {
  width: number;
  height: number;
  fps: number;
}

export interface CameraHandle {
  stream: MediaStream;
  track: MediaStreamTrack;
  /** `track.getSettings()` at the time the camera was opened (recorded in the session, PLAN §5.2). */
  settings: MediaTrackSettings;
  /** Stops all tracks. Safe to call more than once. */
  stop(): void;
  /** Calls `cb` when the track ends on its own (camera unplugged, permission revoked). Returns an unsubscribe function. */
  onEnded(cb: () => void): () => void;
}

const KIND_BY_ERROR_NAME: Record<string, CameraErrorKind> = {
  NotAllowedError: "denied",
  SecurityError: "denied",
  PermissionDeniedError: "denied",
  NotFoundError: "not_found",
  OverconstrainedError: "not_found",
  DevicesNotFoundError: "not_found",
  NotReadableError: "in_use",
  AbortError: "in_use",
  TrackStartError: "in_use",
};

export function mapCameraError(error: unknown): CameraError {
  if (error instanceof CameraError) return error;
  // Read `name` and `message` from any object: a DOMException is not always an `Error` (it is not in
  // every realm, e.g. jsdom).
  const named = (typeof error === "object" && error !== null ? error : {}) as {
    name?: unknown;
    message?: unknown;
  };
  const name = typeof named.name === "string" ? named.name : "";
  const detail =
    name === ""
      ? String(error)
      : `${name}: ${typeof named.message === "string" ? named.message : ""}`;
  return new CameraError(KIND_BY_ERROR_NAME[name] ?? "other", detail);
}

export async function requestCamera(recording: RecordingSettings): Promise<CameraHandle> {
  const mediaDevices = (navigator as { mediaDevices?: MediaDevices }).mediaDevices;
  if (mediaDevices === undefined || typeof mediaDevices.getUserMedia !== "function") {
    throw new CameraError("unsupported", "navigator.mediaDevices.getUserMedia is missing");
  }

  let stream: MediaStream;
  try {
    stream = await mediaDevices.getUserMedia({
      video: {
        width: { ideal: recording.width },
        height: { ideal: recording.height },
        frameRate: { ideal: recording.fps },
        facingMode: "user",
      },
      audio: false,
    });
  } catch (error) {
    throw mapCameraError(error);
  }

  const stopAll = (): void => {
    for (const t of stream.getTracks()) t.stop();
  };
  const track = stream.getVideoTracks()[0];
  if (track === undefined) {
    stopAll();
    throw new CameraError("other", "the stream has no video track");
  }
  const settings = track.getSettings();
  const { width, height } = settings;
  if (width === undefined || height === undefined) {
    stopAll();
    throw new CameraError("other", "the camera did not report its resolution");
  }
  if (width < MIN_CAMERA_WIDTH || height < MIN_CAMERA_HEIGHT) {
    stopAll();
    throw new CameraError("too_small", `${width}x${height}`);
  }

  let stopped = false;
  return {
    stream,
    track,
    settings,
    stop() {
      if (stopped) return;
      stopped = true;
      stopAll();
    },
    onEnded(cb) {
      track.addEventListener("ended", cb);
      return () => track.removeEventListener("ended", cb);
    },
  };
}
