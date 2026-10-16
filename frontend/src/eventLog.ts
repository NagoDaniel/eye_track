// Stream names and event types (PLAN §5.4, §5.9). Mirrored in backend/app/schemas.py;
// protocol.test.ts compares both with shared/protocol.json.

import { now } from "./clock";

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

// --- Typed event log (PLAN §5.9) ---

export interface Size {
  width: number;
  height: number;
}
export interface Rect extends Size {
  x: number;
  y: number;
}
export type DotPhase = "calibration" | "validation";

/** Enum-like values in event data (PLAN §5.6, §12.1); the lists are in shared/protocol.json. */
export const GATE_BLOCKS = ["before_video", "between_videos", "after_last_video"] as const;
export type GateBlock = (typeof GATE_BLOCKS)[number];
export const GATE_REASONS = [
  "ok",
  "few_face_frames",
  "low_brightness",
  "head_or_distance_change",
  "high_error",
  "no_validation",
  "forced_debug",
] as const;
export type GateReason = (typeof GATE_REASONS)[number];
export const SKIP_TARGETS = ["calibration", "validation", "video"] as const;
export type SkipTarget = (typeof SKIP_TARGETS)[number];

/** `data` payload per event type. Types without an entry yet take a free-form record. */
interface KnownEventData {
  session_start: {
    client_info: {
      user_agent: string;
      platform: string;
      hardware_concurrency: number;
      screen: Size;
      device_pixel_ratio: number;
      time_origin: number;
      wall_clock_ms: number;
    };
  };
  viewport_change: {
    viewport: Size;
    screen: Size;
    device_pixel_ratio: number;
    video_rect: Rect | null;
  };
  fullscreen_change: { fullscreen: boolean };
  visibility_change: { state: string };
  window_blur: Record<string, never>;
  window_focus: Record<string, never>;
  calib_dot_show: { phase: DotPhase; id: number; x: number; y: number };
  calib_dot_click: { phase: DotPhase; id: number };
  calib_dot_hide: { phase: DotPhase; id: number };
  video_load: { video_id: string; duration: number; width: number; height: number; rect: Rect };
  video_play: { video_id: string; media_time: number };
  video_pause: { video_id: string; media_time: number };
  video_stall: { video_id: string; media_time: number };
  video_ended: { video_id: string };
  recorder_start: { mime_type: string; bits_per_second: number; settings: Record<string, unknown> };
  recorder_dataavailable: { seq: number; size: number };
  recorder_stop: { chunks: number; bytes: number };
  face_lost: Record<string, never>;
  face_regained: { lost_ms: number };
  calib_gate_result: {
    block: GateBlock;
    attempt: number;
    /** null when the gate was skipped. */
    passed: boolean | null;
    skipped: boolean;
    mean_error_norm: number | null;
    /** Per validation point; null when it had no usable frame. */
    point_errors: Array<number | null>;
    /** Per calibration dot, in dot order. */
    valid_frames_per_dot: number[];
    reason: GateReason;
  };
  debug_skip: { target: SkipTarget };
  client_error: { message: string; stack: string | null; url: string };
}

export type EventDataMap = KnownEventData & {
  [K in Exclude<EventType, keyof KnownEventData>]: Record<string, unknown>;
};

export type EventSink = (record: EventRecord) => void;

/** Stamps events with `t` and hands each record to the sink, in call order. Unknown types do not compile. */
export class EventLog {
  constructor(
    private readonly sink: EventSink,
    private readonly clock: () => number = now,
  ) {}

  log<T extends EventType>(type: T, data: EventDataMap[T]): EventRecord {
    const record: EventRecord = { t: this.clock(), type, data };
    this.sink(record);
    return record;
  }
}
