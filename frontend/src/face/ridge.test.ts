import { describe, expect, it } from "vitest";
import sklearn from "../../../shared/fixtures/ridge_fit.json";
import { MIN_RIDGE_SAMPLES, RIDGE_ALPHA, expandDegree2, fitRidge, predict } from "./ridge";
import type { RidgeModel } from "./ridge";

const SKLEARN_TOLERANCE = 1e-6;
const EXACT_TOLERANCE = 1e-6;
/** Practically no shrinkage, so a noise-free quadratic is recovered exactly. */
const TINY_ALPHA = 1e-10;

/** Small deterministic generator (mulberry32) so the synthetic data is the same on every run. */
function random(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function quadratic(row: readonly number[]): [number, number] {
  const [a = 0, b = 0, c = 0] = row;
  return [
    1000 + 40 * a - 25 * b + 7 * a * a + 3 * a * c - 2 * b * b,
    -300 + 12 * c + 5 * a * b - 9 * c * c + 0.5 * b * c,
  ];
}

describe("expandDegree2", () => {
  it("uses scikit-learn's column order", () => {
    expect(expandDegree2([2, 3, 5])).toEqual([2, 3, 5, 4, 6, 10, 9, 15, 25]);
  });
});

describe("fitRidge", () => {
  it("recovers a known quadratic mapping exactly", () => {
    const next = random(1);
    const features = Array.from({ length: 200 }, () => [next() * 10 - 5, next() * 4, next() * 100]);
    const targets = features.map(quadratic);
    const model = fitRidge(features, targets, TINY_ALPHA);
    for (const row of Array.from({ length: 30 }, () => [
      next() * 10 - 5,
      next() * 4,
      next() * 100,
    ])) {
      const [x, y] = predict(model, row);
      const [expectedX, expectedY] = quadratic(row);
      expect(Math.abs(x - expectedX)).toBeLessThan(EXACT_TOLERANCE);
      expect(Math.abs(y - expectedY)).toBeLessThan(EXACT_TOLERANCE);
    }
  });

  it("defaults to the plan's alpha", () => {
    expect(RIDGE_ALPHA).toBe(1.0);
    const model = fitRidge(
      [[0], [1], [2]],
      [
        [0, 0],
        [1, 1],
        [2, 2],
      ],
    );
    expect(model.alpha).toBe(RIDGE_ALPHA);
  });

  it("matches scikit-learn's pipeline on the stored fixture", () => {
    const targets = sklearn.targets.map((t): [number, number] => [t[0] ?? 0, t[1] ?? 0]);
    const model = fitRidge(sklearn.features, targets, sklearn.alpha);
    expect(sklearn.new_features.length).toBeGreaterThan(0);
    sklearn.new_features.forEach((row, i) => {
      const [x, y] = predict(model, row);
      expect(Math.abs(x - (sklearn.predictions[i]?.[0] ?? Number.NaN))).toBeLessThan(
        SKLEARN_TOLERANCE,
      );
      expect(Math.abs(y - (sklearn.predictions[i]?.[1] ?? Number.NaN))).toBeLessThan(
        SKLEARN_TOLERANCE,
      );
    });
  });

  it("a degree-1 model matches scikit-learn's StandardScaler + Ridge (no polynomial step)", () => {
    const targets = sklearn.targets.map((t): [number, number] => [t[0] ?? 0, t[1] ?? 0]);
    const model = fitRidge(sklearn.features, targets, sklearn.linear_alpha, 1);
    expect(model.degree).toBe(1);
    expect(model.weights[0]).toHaveLength(sklearn.features[0]?.length ?? -1); // no squares, no products
    sklearn.new_features.forEach((row, i) => {
      const [x, y] = predict(model, row);
      expect(Math.abs(x - (sklearn.linear_predictions[i]?.[0] ?? Number.NaN))).toBeLessThan(
        SKLEARN_TOLERANCE,
      );
      expect(Math.abs(y - (sklearn.linear_predictions[i]?.[1] ?? Number.NaN))).toBeLessThan(
        SKLEARN_TOLERANCE,
      );
    });
  });

  it("degree 2 is the default and is stored in the model", () => {
    const model = fitRidge(
      [[1], [2], [3], [4]],
      [
        [1, 1],
        [2, 2],
        [3, 3],
        [4, 4],
      ],
    );
    expect(model.degree).toBe(2);
  });

  it("uses the population std and replaces a zero std by 1", () => {
    const model = fitRidge(
      [
        [1, 5],
        [3, 5],
        [5, 5],
        [7, 5],
      ],
      [
        [0, 0],
        [1, 1],
        [2, 2],
        [3, 3],
      ],
    );
    expect(model.means).toEqual([4, 5]);
    expect(model.stds[0]).toBeCloseTo(Math.sqrt(5), 12);
    expect(model.stds[1]).toBe(1);
  });

  it("is plain JSON", () => {
    const next = random(2);
    const features = Array.from({ length: 40 }, () => [next(), next(), next()]);
    const model = fitRidge(features, features.map(quadratic));
    const copy = JSON.parse(JSON.stringify(model)) as RidgeModel;
    expect(copy).toEqual(model);
    expect(predict(copy, [0.2, 0.4, 0.6])).toEqual(predict(model, [0.2, 0.4, 0.6]));
  });

  it("fails loudly on bad input", () => {
    expect(MIN_RIDGE_SAMPLES).toBeGreaterThan(1);
    expect(() => fitRidge([[1, 2]], [[0, 0]])).toThrow(/at least/);
    expect(() => fitRidge([[1], [2]], [[0, 0]])).toThrow(/targets/);
    expect(() =>
      fitRidge(
        [[1, 2], [3]],
        [
          [0, 0],
          [1, 1],
        ],
      ),
    ).toThrow(/differ/);
    expect(() =>
      fitRidge(
        [[1], [Number.NaN]],
        [
          [0, 0],
          [1, 1],
        ],
      ),
    ).toThrow(/finite/);
    expect(() =>
      fitRidge(
        [[1], [2]],
        [
          [0, 0],
          [1, 1],
        ],
        0,
      ),
    ).toThrow(/alpha/);
  });

  it("throws a clear error on a singular system", () => {
    // Constant column and almost no regularisation: the normal equations have no usable pivot.
    expect(() =>
      fitRidge(
        [[1], [1], [1]],
        [
          [0, 0],
          [1, 1],
          [2, 2],
        ],
        1e-13,
      ),
    ).toThrow(/singular/);
  });

  it("rejects a prediction with the wrong number of features", () => {
    const model = fitRidge(
      [
        [0, 1],
        [1, 2],
        [2, 4],
      ],
      [
        [0, 0],
        [1, 1],
        [2, 2],
      ],
    );
    expect(() => predict(model, [1])).toThrow(/takes 2 features/);
  });
});
