// Study config (PLAN §4.1) and participant API payloads (PLAN §6). Mirrored in
// backend/app/schemas.py; protocol.test.ts checks the shared example payloads against these types.

import type { Stream } from "./eventLog";

export const BROWSER_REQUIREMENTS = [
  "getUserMedia",
  "MediaRecorder",
  "requestVideoFrameCallback",
  "requestFullscreen",
  "IndexedDB",
  "WebAssembly",
] as const;

/** Same pattern as PARTICIPANT_CODE_PATTERN in backend/app/schemas.py (checked in protocol.test.ts). */
export const PARTICIPANT_CODE_PATTERN = /^[A-Z]{2}(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$/;

export interface StudyConfig {
  playlist: string[]; // video ids as strings
  calibration: { points: number; dot_duration_ms: number; margin: number };
  validation: {
    points: number;
    before_video: boolean;
    between_videos: boolean;
    after_last_video: boolean;
  };
  calibration_gate: {
    enabled: boolean;
    max_mean_error_norm: number;
    max_point_error_norm: number;
    min_valid_frames_per_dot: number;
    max_attempts: number;
    on_fail: "continue_flagged" | "end_session";
  };
  playback: { allow_pause: boolean; require_fullscreen: boolean };
  recording: { width: number; height: number; fps: number; chunk_ms: number };
  quality: {
    min_face_ratio_warn: number;
    max_face_ratio_warn: number;
    brightness_min: number;
    brightness_max: number;
    face_ratio_min: number;
    face_ratio_max: number;
    max_yaw_deg: number;
    max_pitch_deg: number;
  };
  inclusion: { max_validation_error_norm: number; min_valid_fraction: number };
  retention: { raw_video_days_after_processing: number | null };
  consent_version: string;
  debug_allowed: boolean;
}

/** GET /api/studies/{code} */
export interface StudyInfo {
  name: string;
  code: string;
  mode: "remote" | "lab";
  consent_version: string;
  consent_text: string;
  browser_requirements: string[];
}

export interface VideoRef {
  id: string;
  url: string;
  sha256: string;
  duration: number;
  /** Frames per second of the stimulus video (the face model's target rate). */
  fps: number;
}

/** POST /api/sessions. The participant code is two letters + birth month + day, e.g. AB0315. */
export interface SessionCreateRequest {
  study_code: string;
  participant_code: string;
  /** Asks for a debug session (default false); allowed only for `debug_allowed` studies or admins. */
  debug?: boolean;
}

export interface SessionCreateResponse {
  session_id: string;
  upload_token: string;
  config: StudyConfig;
  videos: VideoRef[];
}

/** POST /api/sessions/{id}/consent */
export interface ConsentRequest {
  consent_version: string;
}
export interface ConsentResponse {
  status: "consented";
}

/** Reply to PUT /api/sessions/{id}/chunks/{stream}/{seq} */
export interface ChunkAck {
  stream: Stream;
  seq: number;
  size: number;
}

/** POST /api/sessions/{id}/complete: highest seq sent per stream, and what the server misses. */
export interface CompleteRequest {
  last_seq: Partial<Record<Stream, number>>;
  /** true after a page reload mid-session; the server flags the session `interrupted`. */
  interrupted?: boolean;
}
export interface CompleteResponse {
  status: "uploaded" | "incomplete";
  missing: Partial<Record<Stream, number[]>>;
}

/** POST /api/client-errors */
export interface ClientErrorRequest {
  message: string;
  stack?: string | null;
  url?: string | null;
  t?: number | null;
  session_id?: string | null;
}
