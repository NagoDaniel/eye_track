// Building the compact feature record of one processed webcam frame (PLAN §5.4). Pure: the
// MediaPipe and canvas glue lives elsewhere and calls these functions.

import type { FeatureRecord, Landmark } from "../records";
import { SUBSET_MESH_INDICES, SUBSET_SIZE, frameFeatures } from "./features";

/** Coordinates, matrix, blink scores, face ratio and brightness are rounded to this many decimals. */
export const FEATURE_DECIMALS = 5;
const ROUND_FACTOR = 10 ** FEATURE_DECIMALS;

// Rec.709 luma weights.
const LUMA_RED = 0.2126;
const LUMA_GREEN = 0.7152;
const LUMA_BLUE = 0.0722;
const CHANNEL_MAX = 255;
const RGBA_STRIDE = 4;

/** What the face model gave for one frame (the caller converts MediaPipe's output to this). */
export interface FaceResult {
  /** Number of faces found. */
  faces: number;
  /** The full 478-point mesh of the first face, normalised x, y, z. Null when there is no face. */
  landmarks: Landmark[] | null;
  /** The facial transformation matrix, 16 numbers, row-major. Null when there is no face. */
  matrix: number[] | null;
  blinkL: number;
  blinkR: number;
}

export interface FeatureRecordInput {
  t: number;
  webcamMediaTime: number;
  captureTime: number | null;
  presentedFrames: number | null;
  result: FaceResult;
  /** Frame width divided by height (needed for `face_ratio`). */
  aspect: number;
  /** Mean luminance of the face region, 0 to 1; null when it could not be measured. */
  brightness: number | null;
}

export interface FaceBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface RgbaImage {
  data: Uint8ClampedArray;
  width: number;
  height: number;
}

/** Rounds to FEATURE_DECIMALS; never returns -0. */
function round(value: number): number {
  return Math.round(value * ROUND_FACTOR) / ROUND_FACTOR + 0;
}

/** The landmarks of the subset, in `lm` order. A list that already has subset size is returned as is. */
export function subsetOf(landmarks: readonly Landmark[]): Landmark[] {
  if (landmarks.length === SUBSET_SIZE) return [...landmarks];
  return SUBSET_MESH_INDICES.map((meshIndex) => {
    const landmark = landmarks[meshIndex];
    if (landmark === undefined) {
      throw new Error(
        `the mesh has ${landmarks.length} landmarks, landmark ${meshIndex} is missing`,
      );
    }
    return landmark;
  });
}

/**
 * The feature record of one frame. Zero faces gives `face: false` and every measurement null. With
 * several faces the first one is used; the device check looks at `result.faces` separately. A face
 * without landmarks or matrix, and malformed landmarks or matrix, throw: the model must not give
 * half a result. `brightness` may be null (the pixels could not be read; the server accepts that).
 */
export function buildFeatureRecord(input: FeatureRecordInput): FeatureRecord {
  const { result } = input;
  const base = {
    t: input.t,
    webcam_media_time: input.webcamMediaTime,
    capture_time: input.captureTime,
    presented_frames: input.presentedFrames,
  };
  if (result.faces >= 1 && (result.landmarks === null || result.matrix === null)) {
    throw new Error("the face model found a face but gave no landmarks or matrix");
  }
  if (result.faces < 1 || result.landmarks === null || result.matrix === null) {
    return {
      ...base,
      face: false,
      lm: null,
      matrix: null,
      blink_l: null,
      blink_r: null,
      face_ratio: null,
      brightness: null,
    };
  }
  const lm = subsetOf(result.landmarks);
  const features = frameFeatures(lm, result.matrix, input.aspect);
  return {
    ...base,
    face: true,
    lm: lm.map(([x, y, z]): Landmark => [round(x), round(y), round(z)]),
    matrix: result.matrix.map(round),
    blink_l: round(result.blinkL),
    blink_r: round(result.blinkR),
    face_ratio: round(features.faceRatio),
    brightness: input.brightness === null ? null : round(input.brightness),
  };
}

const clamp01 = (value: number): number => Math.min(1, Math.max(0, value));

/**
 * Normalised bounding box of the face landmarks, clamped to the image. Pass the full mesh (the
 * subset is picked out of it) or the subset itself. Throws when there are no landmarks.
 */
export function faceBoxOf(landmarks: readonly Landmark[]): FaceBox {
  const points = subsetOf(landmarks);
  if (points.length === 0) throw new Error("no landmarks");
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const x0 = clamp01(Math.min(...xs));
  const y0 = clamp01(Math.min(...ys));
  const x1 = clamp01(Math.max(...xs));
  const y1 = clamp01(Math.max(...ys));
  return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
}

/**
 * Mean Rec.709 luma (0 to 1) of the box region of an RGBA image. The box is clamped to the image;
 * a box with no pixels in the image gives null.
 */
export function meanLuminance(pixels: RgbaImage, box: FaceBox): number | null {
  const { data, width, height } = pixels;
  if (data.length < width * height * RGBA_STRIDE) throw new Error("the pixel buffer is too small");
  if (!(box.width > 0) || !(box.height > 0)) return null;
  const left = Math.max(0, Math.floor(box.x * width));
  const top = Math.max(0, Math.floor(box.y * height));
  const right = Math.min(width, Math.ceil((box.x + box.width) * width));
  const bottom = Math.min(height, Math.ceil((box.y + box.height) * height));
  if (right <= left || bottom <= top) return null;
  let sum = 0;
  for (let y = top; y < bottom; y++) {
    for (let x = left; x < right; x++) {
      const i = (y * width + x) * RGBA_STRIDE;
      sum +=
        LUMA_RED * (data[i] ?? 0) +
        LUMA_GREEN * (data[i + 1] ?? 0) +
        LUMA_BLUE * (data[i + 2] ?? 0);
    }
  }
  return sum / ((right - left) * (bottom - top) * CHANNEL_MAX);
}
