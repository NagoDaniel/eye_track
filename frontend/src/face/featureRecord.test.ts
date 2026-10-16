import { describe, expect, it } from "vitest";

import fixture from "../../../shared/fixtures/face_frames.json";
import protocol from "../../../shared/protocol.json";
import { FEATURE_MATRIX_SIZE, LANDMARK_COUNT } from "../constants";
import type { Landmark } from "../records";
import {
  FEATURE_DECIMALS,
  type FaceResult,
  buildFeatureRecord,
  faceBoxOf,
  meanLuminance,
} from "./featureRecord";
import { SUBSET_MESH_INDICES, SUBSET_POSITION, SUBSET_SIZE } from "./features";

const MESH_SIZE = 478;
const TOLERANCE = 10 ** -FEATURE_DECIMALS;

interface FixtureFrame {
  lm: Landmark[];
  matrix: number[];
  blink_l: number;
  blink_r: number;
  expected: { face_ratio: number };
}
const frames = fixture.frames as unknown as FixtureFrame[];
const frame0 = frames[0] as FixtureFrame;

/** A full mesh with the subset landmarks at their mesh indices and zeros elsewhere. */
function meshOf(lm: readonly Landmark[]): Landmark[] {
  const mesh: Landmark[] = Array.from({ length: MESH_SIZE }, () => [0, 0, 0]);
  SUBSET_MESH_INDICES.forEach((meshIndex, position) => {
    mesh[meshIndex] = lm[position] as Landmark;
  });
  return mesh;
}

function resultOf(frame: FixtureFrame, faces = 1): FaceResult {
  return {
    faces,
    landmarks: meshOf(frame.lm),
    matrix: frame.matrix,
    blinkL: frame.blink_l,
    blinkR: frame.blink_r,
  };
}

function input(result: FaceResult, brightness: number | null = 0.5) {
  return {
    t: 100,
    webcamMediaTime: 3.2,
    captureTime: 99.5,
    presentedFrames: 96,
    result,
    aspect: fixture.aspect,
    brightness,
  };
}

const NO_FACE: FaceResult = { faces: 0, landmarks: null, matrix: null, blinkL: 0, blinkR: 0 };

describe("SUBSET_MESH_INDICES", () => {
  it("is the inverse of SUBSET_POSITION", () => {
    expect(SUBSET_MESH_INDICES).toHaveLength(SUBSET_SIZE);
    SUBSET_MESH_INDICES.forEach((meshIndex, position) =>
      expect(SUBSET_POSITION.get(meshIndex)).toBe(position),
    );
  });
});

describe("buildFeatureRecord on the real MediaPipe frames", () => {
  it("keeps lm order, matrix, blinks and the face ratio of every frame", () => {
    expect(frames.length).toBeGreaterThan(10);
    for (const frame of frames) {
      const record = buildFeatureRecord(input(resultOf(frame)));
      expect(record.face).toBe(true);
      expect(record.lm).toHaveLength(LANDMARK_COUNT);
      frame.lm.forEach((landmark, i) =>
        landmark.forEach((value, axis) =>
          expect(Math.abs((record.lm?.[i]?.[axis] ?? Number.NaN) - value)).toBeLessThanOrEqual(
            TOLERANCE,
          ),
        ),
      );
      expect(record.matrix).toHaveLength(FEATURE_MATRIX_SIZE);
      frame.matrix.forEach((value, i) =>
        expect(Math.abs((record.matrix?.[i] ?? Number.NaN) - value)).toBeLessThanOrEqual(TOLERANCE),
      );
      expect(Math.abs((record.blink_l ?? Number.NaN) - frame.blink_l)).toBeLessThanOrEqual(
        TOLERANCE,
      );
      expect(Math.abs((record.blink_r ?? Number.NaN) - frame.blink_r)).toBeLessThanOrEqual(
        TOLERANCE,
      );
      expect(Math.abs((record.face_ratio ?? Number.NaN) - frame.expected.face_ratio)).toBeLessThan(
        1e-4,
      );
      expect(record.brightness).toBe(0.5);
    }
  });

  it("copies the timing fields and rounds to the named number of decimals", () => {
    const record = buildFeatureRecord(input(resultOf(frame0), 0.123456789));
    expect(record).toMatchObject({
      t: 100,
      webcam_media_time: 3.2,
      capture_time: 99.5,
      presented_frames: 96,
      brightness: 0.12346,
    });
    const scale = 10 ** FEATURE_DECIMALS;
    for (const value of [...(record.lm?.flat() ?? []), ...(record.matrix ?? [])]) {
      expect(Math.abs(value * scale - Math.round(value * scale))).toBeLessThan(1e-6);
    }
  });

  it("uses the first face when there are several", () => {
    const one = buildFeatureRecord(input(resultOf(frame0, 1)));
    const two = buildFeatureRecord(input(resultOf(frame0, 2)));
    expect(two).toEqual(one);
    expect(two.face).toBe(true);
  });

  it("accepts a null brightness and null webcam-frame values", () => {
    const record = buildFeatureRecord({
      ...input(resultOf(frame0), null),
      captureTime: null,
      presentedFrames: null,
    });
    expect(record).toMatchObject({ brightness: null, capture_time: null, presented_frames: null });
  });

  it("throws on a mesh that is too short or a matrix of the wrong size", () => {
    const short = { ...resultOf(frame0), landmarks: meshOf(frame0.lm).slice(0, 100) };
    expect(() => buildFeatureRecord(input(short))).toThrow(/missing/);
    const badMatrix = { ...resultOf(frame0), matrix: [1, 2, 3] };
    expect(() => buildFeatureRecord(input(badMatrix))).toThrow(/16 numbers/);
  });
});

describe("buildFeatureRecord without a face", () => {
  it("has every measurement null", () => {
    expect(buildFeatureRecord(input(NO_FACE))).toEqual({
      t: 100,
      webcam_media_time: 3.2,
      capture_time: 99.5,
      presented_frames: 96,
      face: false,
      lm: null,
      matrix: null,
      blink_l: null,
      blink_r: null,
      face_ratio: null,
      brightness: null,
    });
  });

  it("a face without landmarks or matrix is an error, not a silent no-face frame", () => {
    expect(() => buildFeatureRecord(input({ ...NO_FACE, faces: 1 }))).toThrow(
      /no landmarks or matrix/,
    );
    expect(() => buildFeatureRecord(input({ ...resultOf(frame0), matrix: null }))).toThrow();
    expect(() => buildFeatureRecord(input({ ...resultOf(frame0), landmarks: null }))).toThrow();
  });

  it("no faces with null landmarks is a normal no-face frame", () => {
    expect(buildFeatureRecord(input(NO_FACE)).face).toBe(false);
  });

  it("a face with an unknown brightness has exactly the shape of the shared example the server accepts", () => {
    const record = buildFeatureRecord(input(resultOf(frame0), null));
    const example = protocol.examples.feature_record_unmeasured_brightness;
    expect(Object.keys(record).sort()).toEqual(Object.keys(example).sort());
    expect(record.face).toBe(true);
    expect(record.brightness).toBeNull();
    expect(example.face).toBe(true);
    expect(example.brightness).toBeNull();
    expect(record.lm).toHaveLength(example.lm.length);
    expect(record.matrix).toHaveLength(example.matrix.length);
  });
});

describe("the shared examples in protocol.json", () => {
  const examples = protocol.examples;

  it("have the same keys as our records", () => {
    const withFace = buildFeatureRecord(input(resultOf(frame0)));
    expect(Object.keys(withFace).sort()).toEqual(Object.keys(examples.feature_record).sort());
    expect(examples.feature_record.lm).toHaveLength(LANDMARK_COUNT);
    expect(examples.feature_record.matrix).toHaveLength(FEATURE_MATRIX_SIZE);
  });

  it("equal our no-face record for the same timing", () => {
    const example = examples.feature_record_no_face;
    const record = buildFeatureRecord({
      t: example.t,
      webcamMediaTime: example.webcam_media_time,
      captureTime: example.capture_time,
      presentedFrames: example.presented_frames,
      result: NO_FACE,
      aspect: 16 / 9,
      brightness: null,
    });
    expect(record).toEqual(example);
  });
});

describe("faceBoxOf", () => {
  it("is the bounding box of the real subset landmarks, inside the image", () => {
    const box = faceBoxOf(frame0.lm);
    const xs = frame0.lm.map((p) => p[0]);
    const ys = frame0.lm.map((p) => p[1]);
    expect(box.x).toBeCloseTo(Math.min(...xs), 10);
    expect(box.y).toBeCloseTo(Math.min(...ys), 10);
    expect(box.width).toBeCloseTo(Math.max(...xs) - Math.min(...xs), 10);
    expect(box.height).toBeCloseTo(Math.max(...ys) - Math.min(...ys), 10);
    expect(box.width).toBeGreaterThan(0.05);
    expect(box.x + box.width).toBeLessThanOrEqual(1);
  });

  it("gives the same box for the full mesh as for the subset", () => {
    expect(faceBoxOf(meshOf(frame0.lm))).toEqual(faceBoxOf(frame0.lm));
  });

  it("clamps to the image", () => {
    const points: Landmark[] = frame0.lm.map((p, i) => (i === 0 ? [-0.2, 1.3, 0] : p));
    const box = faceBoxOf(points);
    expect(box.x).toBe(0);
    expect(box.y + box.height).toBe(1);
  });
});

describe("meanLuminance", () => {
  function image(width: number, height: number, rgb: (x: number, y: number) => number[]) {
    const data = new Uint8ClampedArray(width * height * 4);
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const [r = 0, g = 0, b = 0] = rgb(x, y);
        data.set([r, g, b, 255], (y * width + x) * 4);
      }
    }
    return { data, width, height };
  }
  const whole = { x: 0, y: 0, width: 1, height: 1 };

  it("is 1 for white, 0 for black", () => {
    expect(
      meanLuminance(
        image(4, 4, () => [255, 255, 255]),
        whole,
      ),
    ).toBeCloseTo(1, 10);
    expect(
      meanLuminance(
        image(4, 4, () => [0, 0, 0]),
        whole,
      ),
    ).toBe(0);
  });

  it("weights the channels with Rec.709", () => {
    expect(
      meanLuminance(
        image(2, 2, () => [255, 0, 0]),
        whole,
      ),
    ).toBeCloseTo(0.2126, 10);
    expect(
      meanLuminance(
        image(2, 2, () => [0, 255, 0]),
        whole,
      ),
    ).toBeCloseTo(0.7152, 10);
    expect(
      meanLuminance(
        image(2, 2, () => [0, 0, 255]),
        whole,
      ),
    ).toBeCloseTo(0.0722, 10);
  });

  it("measures only the box", () => {
    const half = image(4, 4, (x) => (x < 2 ? [255, 255, 255] : [0, 0, 0]));
    expect(meanLuminance(half, { x: 0, y: 0, width: 0.5, height: 1 })).toBeCloseTo(1, 10);
    expect(meanLuminance(half, { x: 0.5, y: 0, width: 0.5, height: 1 })).toBe(0);
    expect(meanLuminance(half, whole)).toBeCloseTo(0.5, 10);
  });

  it("clamps a box that sticks out and gives null for an empty one", () => {
    const white = image(4, 4, () => [255, 255, 255]);
    expect(meanLuminance(white, { x: -1, y: -1, width: 3, height: 3 })).toBeCloseTo(1, 10);
    expect(meanLuminance(white, { x: 0.2, y: 0.2, width: 0, height: 0.5 })).toBeNull();
    expect(meanLuminance(white, { x: 2, y: 2, width: 1, height: 1 })).toBeNull();
  });

  it("throws on a buffer that is too small", () => {
    const small = { data: new Uint8ClampedArray(8), width: 4, height: 4 };
    expect(() => meanLuminance(small, whole)).toThrow(/too small/);
  });
});
