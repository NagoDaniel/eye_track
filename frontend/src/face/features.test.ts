import { describe, expect, it } from "vitest";

import fixture from "../../../shared/fixtures/face_frames.json";
import landmarkSubset from "../../../shared/landmark_subset.json";
import protocol from "../../../shared/protocol.json";
import { LANDMARK_COUNT } from "../constants";
import type { Landmark } from "../records";
import {
  type EyeLandmarks,
  LEFT_EYE,
  RIGHT_EYE,
  SUBSET_POSITION,
  SUBSET_SIZE,
  frameFeatures,
  headPose,
} from "./features";

const ASPECT = 16 / 9;
const IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];

type Matrix3 = number[][];

function at<T>(items: readonly T[], index: number): T {
  const item = items[index];
  if (item === undefined) throw new Error(`no item at ${index}`);
  return item;
}

function position(meshIndex: number): number {
  const found = SUBSET_POSITION.get(meshIndex);
  if (found === undefined) throw new Error(`landmark ${meshIndex} is not in the subset`);
  return found;
}

/** R = Rz(roll) Ry(yaw) Rx(pitch), angles in degrees. */
function rotation(yaw: number, pitch: number, roll: number): Matrix3 {
  const [y, p, r] = [yaw, pitch, roll].map((a) => (a * Math.PI) / 180) as [number, number, number];
  const [cy, sy, cp, sp, cr, sr] = [
    Math.cos(y),
    Math.sin(y),
    Math.cos(p),
    Math.sin(p),
    Math.cos(r),
    Math.sin(r),
  ] as const;
  const rx = [
    [1, 0, 0],
    [0, cp, -sp],
    [0, sp, cp],
  ];
  const ry = [
    [cy, 0, sy],
    [0, 1, 0],
    [-sy, 0, cy],
  ];
  const rz = [
    [cr, -sr, 0],
    [sr, cr, 0],
    [0, 0, 1],
  ];
  const mul = (a: Matrix3, b: Matrix3): Matrix3 =>
    [0, 1, 2].map((i) =>
      [0, 1, 2].map((j) => [0, 1, 2].reduce((s, k) => s + at(at(a, i), k) * at(at(b, k), j), 0)),
    );
  return mul(rz, mul(ry, rx));
}

function matrixOf(rot: Matrix3, t: [number, number, number] = [0, 0, 0], scale = 1): number[] {
  const rows = [0, 1, 2].map((i) => [...at(rot, i).map((v) => v * scale), at(t, i)]);
  return [...rows.flat(), 0, 0, 0, 1];
}

interface EyeOptions {
  angleDeg?: number;
  irisOffset?: [number, number];
  openRatio?: number;
  centre?: [number, number];
  width?: number;
}

/** Put one eye into `lm`; the geometry is given in pixel-proportional units `(x * ASPECT, y)`. */
function placeEye(
  lm: Landmark[],
  eye: EyeLandmarks,
  centre: [number, number],
  options: EyeOptions = {},
): void {
  const { angleDeg = 0, irisOffset = [0, 0], openRatio = 0.3, width = 0.08 } = options;
  const a = (angleDeg * Math.PI) / 180;
  const ex = [Math.cos(a), Math.sin(a)] as const;
  const ey = [-Math.sin(a), Math.cos(a)] as const;
  const put = (meshIndex: number, u: number, v: number): void => {
    const px = centre[0] + width * (u * ex[0] + v * ey[0]);
    const py = centre[1] + width * (u * ex[1] + v * ey[1]);
    lm[position(meshIndex)] = [px / ASPECT, py, 0];
  };
  put(eye.cornerA, -0.5, 0);
  put(eye.cornerB, 0.5, 0);
  put(eye.upperLid, 0, -openRatio / 2);
  put(eye.lowerLid, 0, openRatio / 2);
  const ring: Array<[number, number]> = [
    [0, 0],
    [0.05, 0],
    [-0.05, 0],
    [0, 0.05],
    [0, -0.05],
  ];
  eye.iris.forEach((meshIndex, k) => {
    const [du, dv] = at(ring, k);
    put(meshIndex, irisOffset[0] + du, irisOffset[1] + dv);
  });
}

function face(right: EyeOptions = {}, left: EyeOptions = {}): Landmark[] {
  const lm: Landmark[] = Array.from({ length: SUBSET_SIZE }, () => [0.5, 0.5, 0]);
  placeEye(lm, RIGHT_EYE, [0.4 * ASPECT, 0.45], right);
  placeEye(lm, LEFT_EYE, [0.6 * ASPECT, 0.45], left);
  return lm;
}

const near = (actual: readonly number[], expected: readonly number[], digits = 9): void => {
  expect(actual).toHaveLength(expected.length);
  expected.forEach((value, i) => expect(actual[i]).toBeCloseTo(value, digits));
};

describe("eye-local iris position, openness, face scale", () => {
  it("a centred iris is at the origin", () => {
    const f = frameFeatures(face(), IDENTITY, ASPECT);
    near(f.irisL, [0, 0]);
    near(f.irisR, [0, 0]);
  });

  it("the iris offset is measured in eye widths along the eye axes", () => {
    const f = frameFeatures(
      face({ irisOffset: [0.25, 0.1] }, { irisOffset: [-0.2, -0.05] }),
      IDENTITY,
      ASPECT,
    );
    near(f.irisR, [0.25, 0.1]);
    near(f.irisL, [-0.2, -0.05]);
  });

  it.each([-40, 15, 90])(
    "a tilted eye (%d degrees) gives the same local coordinates",
    (angleDeg) => {
      const f = frameFeatures(
        face({ angleDeg, irisOffset: [0.25, 0.1] }, { angleDeg }),
        IDENTITY,
        ASPECT,
      );
      near(f.irisR, [0.25, 0.1]);
      expect(f.openR).toBeCloseTo(0.3, 9);
    },
  );

  it("the eye width scales the result, not the position", () => {
    const lm = face();
    placeEye(lm, RIGHT_EYE, [0.4 * ASPECT, 0.45], {
      width: 0.16,
      irisOffset: [0.1, 0],
    });
    near(frameFeatures(lm, IDENTITY, ASPECT).irisR, [0.1, 0]);
  });

  it("openness is the lid distance over the eye width", () => {
    const f = frameFeatures(face({ openRatio: 0.35 }, { openRatio: 0 }), IDENTITY, ASPECT);
    expect(f.openR).toBeCloseTo(0.35, 9);
    expect(f.openL).toBeCloseTo(0, 9);
  });

  it("face ratio is the eye distance over the frame width", () => {
    expect(frameFeatures(face(), IDENTITY, ASPECT).faceRatio).toBeCloseTo(0.2, 9);
    const lm = face();
    placeEye(lm, LEFT_EYE, [0.6 * ASPECT, 0.55]);
    const expected = Math.hypot(0.2 * ASPECT, 0.1) / ASPECT;
    expect(frameFeatures(lm, IDENTITY, ASPECT).faceRatio).toBeCloseTo(expected, 9);
  });

  it("the aspect ratio matters", () => {
    const lm = face();
    placeEye(lm, RIGHT_EYE, [0.4 * ASPECT, 0.45], {
      angleDeg: 30,
      irisOffset: [0.2, 0.1],
    });
    near(frameFeatures(lm, IDENTITY, ASPECT).irisR, [0.2, 0.1]);
    const wrong = frameFeatures(lm, IDENTITY, 1).irisR;
    expect(Math.abs(wrong[0] - 0.2)).toBeGreaterThan(1e-3);
  });
});

describe("head pose", () => {
  it("the identity matrix is a frontal head", () => {
    const pose = headPose(IDENTITY);
    near([pose.yaw, pose.pitch, pose.roll, pose.tx, pose.ty, pose.tz], [0, 0, 0, 0, 0, 0], 12);
  });

  it.each([
    [0, 0, 0],
    [25, 0, 0],
    [0, -15, 0],
    [0, 0, 30],
    [-20, 10, 5],
    [60, -30, 15],
    [-45, 20, -35],
  ] as const)("angles round trip (yaw %d, pitch %d, roll %d)", (yaw, pitch, roll) => {
    const pose = headPose(matrixOf(rotation(yaw, pitch, roll), [1.5, -3, -44]));
    near(
      [pose.yaw, pose.pitch, pose.roll, pose.tx, pose.ty, pose.tz],
      [yaw, pitch, roll, 1.5, -3, -44],
    );
  });

  it("a scaled matrix gives the same angles", () => {
    const pose = headPose(matrixOf(rotation(20, -10, 5), [0, 0, 0], 1.7));
    near([pose.yaw, pose.pitch, pose.roll], [20, -10, 5]);
  });

  it("pitch is positive when the head tilts down, yaw when the face turns to the image right", () => {
    expect(headPose(matrixOf(rotation(0, 20, 0))).pitch).toBeGreaterThan(0);
    expect(headPose(matrixOf(rotation(20, 0, 0))).yaw).toBeGreaterThan(0);
  });

  it.each([
    ["short", new Array<number>(15).fill(1)],
    ["long", new Array<number>(17).fill(1)],
    ["degenerate", new Array<number>(16).fill(0)],
  ])("a %s matrix is an error", (_name, bad) => {
    expect(() => headPose(bad)).toThrow();
  });

  it("bad landmark counts, aspect ratios and eye corners are errors", () => {
    expect(() => frameFeatures(face().slice(0, -1), IDENTITY, ASPECT)).toThrow(/landmarks/);
    expect(() => frameFeatures(face(), IDENTITY, 0)).toThrow(/aspect/);
    const lm = face();
    lm[position(RIGHT_EYE.cornerB)] = [...at(lm, position(RIGHT_EYE.cornerA))] as Landmark;
    expect(() => frameFeatures(lm, IDENTITY, ASPECT)).toThrow(/corners/);
  });
});

describe("the landmark subset", () => {
  it("is the shared file", () => {
    expect(SUBSET_SIZE).toBe(LANDMARK_COUNT);
    expect(SUBSET_SIZE).toBe(Object.values(landmarkSubset.groups).flat().length);
  });
  it("has the landmarks the features need", () => {
    for (const eye of [RIGHT_EYE, LEFT_EYE]) {
      for (const i of [eye.cornerA, eye.cornerB, eye.upperLid, eye.lowerLid, ...eye.iris]) {
        expect(SUBSET_POSITION.has(i)).toBe(true);
      }
    }
  });
});

// The TypeScript and Python implementations must agree within 1e-4 on real MediaPipe frames
// (PLAN §5.6 step 2). The expected numbers in the fixture were written by the Python code.
describe("real MediaPipe frames (shared/fixtures/face_frames.json)", () => {
  const CROSS_LANGUAGE_TOLERANCE = 1e-4;

  it("is real output of the clip", () => {
    expect(fixture.frames.length).toBeGreaterThanOrEqual(40);
    for (const frame of fixture.frames) {
      expect(frame.lm).toHaveLength(protocol.landmark_count);
      expect(frame.matrix).toHaveLength(protocol.feature_matrix_size);
    }
  });

  it("gives the numbers the Python implementation gives", () => {
    for (const frame of fixture.frames) {
      const got = frameFeatures(frame.lm as Landmark[], frame.matrix, fixture.aspect);
      const e = frame.expected;
      const pairs: Array<[string, number, number]> = [
        ["iris_l x", got.irisL[0], at(e.iris_l, 0)],
        ["iris_l y", got.irisL[1], at(e.iris_l, 1)],
        ["iris_r x", got.irisR[0], at(e.iris_r, 0)],
        ["iris_r y", got.irisR[1], at(e.iris_r, 1)],
        ["open_l", got.openL, e.open_l],
        ["open_r", got.openR, e.open_r],
        ["yaw", got.yaw, e.yaw],
        ["pitch", got.pitch, e.pitch],
        ["roll", got.roll, e.roll],
        ["tx", got.tx, e.tx],
        ["ty", got.ty, e.ty],
        ["tz", got.tz, e.tz],
        ["face_ratio", got.faceRatio, e.face_ratio],
      ];
      for (const [name, actual, expected] of pairs) {
        expect(Math.abs(actual - expected), `frame ${frame.frame} ${name}`).toBeLessThan(
          CROSS_LANGUAGE_TOLERANCE,
        );
      }
    }
  });

  it("looks like a person facing the screen, with real blinks", () => {
    let blinking = 0;
    for (const frame of fixture.frames) {
      const f = frameFeatures(frame.lm as Landmark[], frame.matrix, fixture.aspect);
      expect(Math.abs(f.yaw)).toBeLessThan(25);
      expect(f.tz).toBeLessThan(-25);
      expect(Math.abs(f.irisL[0])).toBeLessThan(0.5);
      if (frame.blink_l > 0.5 && frame.blink_r > 0.5) {
        blinking++;
        expect(f.openL + f.openR).toBeLessThan(0.3);
      }
    }
    expect(blinking).toBeGreaterThan(0);
  });
});
