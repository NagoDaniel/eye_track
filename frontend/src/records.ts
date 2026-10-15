// Records of the `features` and `stimulus_frames` streams (PLAN §5.3, §5.4). One JSON object per
// line, gzipped per chunk. Mirrored in backend/app/schemas.py; protocol.test.ts checks the shared
// examples against these types.

/** x, y, z of one landmark, normalised image coordinates as MediaPipe reports them. */
export type Landmark = [number, number, number];

/**
 * A frame the live face model processed. `t` is `performance.now()` when the result was produced;
 * `webcam_media_time`, `capture_time` and `presented_frames` come from `requestVideoFrameCallback`
 * on the hidden webcam preview. Without a face every measurement is null.
 */
export interface FeatureRecord {
  t: number;
  webcam_media_time: number;
  capture_time: number | null;
  presented_frames: number | null;
  face: boolean;
  /** LANDMARK_COUNT landmarks in the order of shared/landmark_subset.json. */
  lm: Landmark[] | null;
  /** FEATURE_MATRIX_SIZE numbers: the facial transformation matrix. */
  matrix: number[] | null;
  blink_l: number | null;
  blink_r: number | null;
  /** Interocular distance as a fraction of the frame width. */
  face_ratio: number | null;
  /** Mean luminance of the face region, 0 to 1. */
  brightness: number | null;
}

/** A presented frame of the stimulus video; the `video_*` events say which video. */
export interface StimulusFrameRecord {
  t: number;
  expected_display_time: number;
  media_time: number;
  presented_frames: number;
}
