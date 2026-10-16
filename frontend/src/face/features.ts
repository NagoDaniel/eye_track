// Pipeline A frame features (PLAN §8.4), the TypeScript twin of backend/worker/pipeline/features_a.py.
// Both must give the same numbers (within 1e-4) on the same input; both are tested on the frames in
// shared/fixtures/face_frames.json (PLAN §5.6 step 2). Change one, change both.
//
// Input: the `lm` and `matrix` of a feature record (PLAN §5.4) and the frame's aspect ratio
// (width / height). Landmarks are normalised image coordinates; to measure distances and angles they
// are first scaled to `(x * aspect, y)`, so a unit step is the same in both directions.
//
// Definitions (subject's left/right, as MediaPipe names them):
// - Eye-local frame: origin at the midpoint of the two eye corners, x-axis along the corner-to-corner
//   vector (for both eyes pointing towards the image's right when the head is upright), y-axis the
//   x-axis turned a quarter turn towards the image's bottom, both scaled by the eye width.
// - `iris`: mean of the five iris landmarks in that frame, as [x, y].
// - `open`: distance between the middle upper and lower lid landmarks, divided by the eye width.
// - Head pose from the facial transformation matrix (16 numbers, row-major, translation in indices
//   3, 7 and 11, in cm): angles in degrees from R = Rz(roll) Ry(yaw) Rx(pitch). Positive pitch: head
//   tilted down; positive yaw: face turned towards the image's right; positive roll:
//   counter-clockwise in the image. `tx`, `ty`, `tz` is the translation.
// - `face_ratio`: distance between the two eye midpoints as a fraction of the frame width.

import landmarkSubset from "../../../shared/landmark_subset.json";
import { FEATURE_MATRIX_SIZE } from "../constants";
import type { Landmark } from "../records";

/** Mesh index to position in `lm`: groups in file order, each group in its order. */
function loadSubset(): Map<number, number> {
  const positions = new Map<number, number>();
  for (const group of Object.values(landmarkSubset.groups)) {
    for (const meshIndex of group) positions.set(meshIndex, positions.size);
  }
  return positions;
}

export const SUBSET_POSITION: ReadonlyMap<number, number> = loadSubset();
export const SUBSET_SIZE = SUBSET_POSITION.size;
/** Mesh index at each position of `lm` (the inverse of `SUBSET_POSITION`). */
export const SUBSET_MESH_INDICES: readonly number[] = [...SUBSET_POSITION.keys()];

export interface EyeLandmarks {
  cornerA: number;
  cornerB: number;
  upperLid: number;
  lowerLid: number;
  iris: readonly number[];
}

// Mesh indices (PLAN §5.4 subset). Corners run towards the image's right.
export const RIGHT_EYE: EyeLandmarks = {
  cornerA: 33,
  cornerB: 133,
  upperLid: 159,
  lowerLid: 145,
  iris: [468, 469, 470, 471, 472],
};
export const LEFT_EYE: EyeLandmarks = {
  cornerA: 362,
  cornerB: 263,
  upperLid: 386,
  lowerLid: 374,
  iris: [473, 474, 475, 476, 477],
};

export type Point = readonly [number, number];

export interface FrameFeatures {
  irisL: Point;
  irisR: Point;
  openL: number;
  openR: number;
  yaw: number;
  pitch: number;
  roll: number;
  tx: number;
  ty: number;
  tz: number;
  faceRatio: number;
}

interface EyeResult {
  iris: Point;
  openness: number;
  centre: Point;
}

function eyeFeatures(lm: readonly Landmark[], aspect: number, eye: EyeLandmarks): EyeResult {
  const point = (meshIndex: number): Point => {
    const position = SUBSET_POSITION.get(meshIndex);
    const landmark = position === undefined ? undefined : lm[position];
    if (landmark === undefined) throw new Error(`landmark ${meshIndex} is missing`);
    return [landmark[0] * aspect, landmark[1]];
  };
  const a = point(eye.cornerA);
  const b = point(eye.cornerB);
  const centre: Point = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const width = Math.hypot(dx, dy);
  if (width === 0) throw new Error("the eye corners coincide");
  const ex = dx / width;
  const ey = dy / width;
  const irisPoints = eye.iris.map(point);
  const ix = irisPoints.reduce((sum, p) => sum + p[0], 0) / irisPoints.length - centre[0];
  const iy = irisPoints.reduce((sum, p) => sum + p[1], 0) / irisPoints.length - centre[1];
  // x-axis (ex, ey); y-axis turned towards the image's bottom: (-ey, ex)
  const iris: Point = [(ix * ex + iy * ey) / width, (-ix * ey + iy * ex) / width];
  const upper = point(eye.upperLid);
  const lower = point(eye.lowerLid);
  const openness = Math.hypot(upper[0] - lower[0], upper[1] - lower[1]) / width;
  return { iris, openness, centre };
}

export interface HeadPose {
  yaw: number;
  pitch: number;
  roll: number;
  tx: number;
  ty: number;
  tz: number;
}

const toDegrees = (radians: number): number => (radians * 180) / Math.PI;

/** Yaw, pitch, roll (degrees) and translation of a row-major 4x4 matrix; its scale is removed first. */
export function headPose(matrix: readonly number[]): HeadPose {
  if (matrix.length !== FEATURE_MATRIX_SIZE) {
    throw new Error(`the matrix needs ${FEATURE_MATRIX_SIZE} numbers, got ${matrix.length}`);
  }
  const at = (row: number, col: number): number => matrix[row * 4 + col] ?? Number.NaN;
  const r = [0, 1, 2].map((row) => [0, 1, 2].map((col) => at(row, col)));
  for (let col = 0; col < 3; col++) {
    const norm = Math.sqrt([0, 1, 2].reduce((sum, row) => sum + (r[row]?.[col] ?? 0) ** 2, 0));
    if (norm === 0) throw new Error("the rotation part of the matrix is degenerate");
    for (let row = 0; row < 3; row++) (r[row] as number[])[col] = (r[row]?.[col] ?? 0) / norm;
  }
  const m = (row: number, col: number): number => r[row]?.[col] ?? Number.NaN;
  return {
    yaw: toDegrees(Math.asin(Math.max(-1, Math.min(1, -m(2, 0))))),
    pitch: toDegrees(Math.atan2(m(2, 1), m(2, 2))),
    roll: toDegrees(Math.atan2(m(1, 0), m(0, 0))),
    tx: at(0, 3),
    ty: at(1, 3),
    tz: at(2, 3),
  };
}

/** The Pipeline A features of one frame with a face. Throws on bad input. */
export function frameFeatures(
  lm: readonly Landmark[],
  matrix: readonly number[],
  aspect: number,
): FrameFeatures {
  if (lm.length !== SUBSET_SIZE)
    throw new Error(`lm needs ${SUBSET_SIZE} landmarks, got ${lm.length}`);
  if (!(aspect > 0)) throw new Error("aspect must be positive");
  const right = eyeFeatures(lm, aspect, RIGHT_EYE);
  const left = eyeFeatures(lm, aspect, LEFT_EYE);
  const pose = headPose(matrix);
  const between = Math.hypot(left.centre[0] - right.centre[0], left.centre[1] - right.centre[1]);
  return {
    irisL: left.iris,
    irisR: right.iris,
    openL: left.openness,
    openR: right.openness,
    ...pose,
    faceRatio: between / aspect,
  };
}
