// Stream names and event types (PLAN §5.4, §5.9). Mirrored in backend/app/schemas.py;
// protocol.test.ts compares both with shared/protocol.json.

export const STREAMS = ["webcam", "features", "events", "stimulus_frames"] as const;
export type Stream = (typeof STREAMS)[number];

export const EVENT_TYPES = [
  "session_start",
  "viewport_change",
  "fullscreen_change",
  "visibility_change",
  "window_blur",
  "window_focus",
  "calib_dot_show",
  "calib_dot_click",
  "calib_dot_hide",
  "calib_gate_result",
  "video_load",
  "video_play",
  "video_pause",
  "video_ended",
  "video_stall",
  "face_lost",
  "face_regained",
  "recorder_start",
  "recorder_dataavailable",
  "recorder_stop",
  "debug_skip",
  "client_error",
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

/** One line of the `events` stream: `t` is `performance.now()` in ms (PLAN §5.3). */
export interface EventRecord {
  t: number;
  type: EventType;
  data: Record<string, unknown>;
}
