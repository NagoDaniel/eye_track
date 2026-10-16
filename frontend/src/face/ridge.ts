// Closed-form ridge regression for the browser calibration gate (PLAN §5.6 step 3).
//
// Same model as scikit-learn
//   make_pipeline(StandardScaler(), PolynomialFeatures(degree=2, include_bias=False),
//                 Ridge(alpha=1.0, fit_intercept=True))
// fitted once for the x target and once for the y target. A model of `degree` 1 leaves out the
// polynomial step (a plain linear ridge). The numbers are compared with scikit-learn in ridge.test.ts
// (fixture made by scripts/make_ridge_fixture.py).

/** Fixed regularisation of the browser gate fit (PLAN §5.6: α = 1.0). */
export const RIDGE_ALPHA = 1.0;
/** Fewest samples a fit accepts: with one sample the centred system carries no information. */
export const MIN_RIDGE_SAMPLES = 2;
/** A Cholesky pivot at or below this means the system is not positive definite (singular). */
const MIN_CHOLESKY_PIVOT = 1e-12;

/** Degree of the polynomial expansion: 1 is a plain linear model, 2 adds squares and products. */
export type RidgeDegree = 1 | 2;

/** Plain JSON-serialisable parameters of a fitted model. `weights[0]` is x, `weights[1]` is y. */
export interface RidgeModel {
  alpha: number;
  degree: RidgeDegree;
  /** Per input column. */
  means: number[];
  /** Per input column, population std (ddof = 0); 0 is replaced by 1. */
  stds: number[];
  /** Per output, one weight per expanded column. */
  weights: [number[], number[]];
  intercepts: [number, number];
}

/** Standardise with the stored means and stds. */
function standardise(
  row: readonly number[],
  means: readonly number[],
  stds: readonly number[],
): number[] {
  return row.map((value, column) => (value - (means[column] ?? 0)) / (stds[column] ?? 1));
}

/**
 * Degree-2 expansion without bias, in scikit-learn's column order: the original columns, then
 * x0*x0, x0*x1, ..., x0*x(d-1), x1*x1, x1*x2, ...
 */
export function expandDegree2(row: readonly number[]): number[] {
  const expanded = [...row];
  for (let i = 0; i < row.length; i++) {
    for (let j = i; j < row.length; j++) {
      expanded.push((row[i] ?? 0) * (row[j] ?? 0));
    }
  }
  return expanded;
}

function expand(row: readonly number[], degree: RidgeDegree): number[] {
  return degree === 2 ? expandDegree2(row) : [...row];
}

/** Solve A w = b for symmetric positive definite A (Cholesky); b holds several right-hand sides. */
function solveCholesky(a: number[][], rightHandSides: number[][]): number[][] {
  const n = a.length;
  const l: number[][] = Array.from({ length: n }, () => new Array<number>(n).fill(0));
  for (let i = 0; i < n; i++) {
    const rowI = l[i] as number[];
    for (let j = 0; j <= i; j++) {
      const rowJ = l[j] as number[];
      let sum = (a[i] as number[])[j] as number;
      for (let k = 0; k < j; k++) sum -= (rowI[k] as number) * (rowJ[k] as number);
      if (i === j) {
        if (!(sum > MIN_CHOLESKY_PIVOT)) {
          throw new Error("ridge: the normal equations are singular");
        }
        rowI[i] = Math.sqrt(sum);
      } else {
        rowI[j] = sum / (rowJ[j] as number);
      }
    }
  }
  return rightHandSides.map((b) => {
    // Forward substitution L z = b, then back substitution Lᵀ w = z.
    const z = new Array<number>(n).fill(0);
    for (let i = 0; i < n; i++) {
      let sum = b[i] as number;
      for (let k = 0; k < i; k++) sum -= ((l[i] as number[])[k] as number) * (z[k] as number);
      z[i] = sum / ((l[i] as number[])[i] as number);
    }
    const w = new Array<number>(n).fill(0);
    for (let i = n - 1; i >= 0; i--) {
      let sum = z[i] as number;
      for (let k = i + 1; k < n; k++) sum -= ((l[k] as number[])[i] as number) * (w[k] as number);
      w[i] = sum / ((l[i] as number[])[i] as number);
    }
    return w;
  });
}

function columnMeans(rows: readonly (readonly number[])[], width: number): number[] {
  const sums = new Array<number>(width).fill(0);
  for (const row of rows)
    row.forEach((value, column) => (sums[column] = (sums[column] ?? 0) + value));
  return sums.map((sum) => sum / rows.length);
}

function checkInput(features: number[][], targets: Array<[number, number]>): number {
  if (features.length !== targets.length) {
    throw new Error(`ridge: ${features.length} feature rows but ${targets.length} targets`);
  }
  if (features.length < MIN_RIDGE_SAMPLES) {
    throw new Error(`ridge: need at least ${MIN_RIDGE_SAMPLES} samples, got ${features.length}`);
  }
  const width = (features[0] as number[]).length;
  if (width === 0) throw new Error("ridge: the feature rows are empty");
  for (const row of features) {
    if (row.length !== width) throw new Error("ridge: the feature rows differ in length");
    if (!row.every(Number.isFinite)) throw new Error("ridge: a feature is not a finite number");
  }
  if (!targets.every(([x, y]) => Number.isFinite(x) && Number.isFinite(y))) {
    throw new Error("ridge: a target is not a finite number");
  }
  return width;
}

/** Fit the pipeline on `features` (one row per sample) and `targets` ([x, y] per sample). */
export function fitRidge(
  features: number[][],
  targets: Array<[number, number]>,
  alpha: number = RIDGE_ALPHA,
  degree: RidgeDegree = 2,
): RidgeModel {
  if (!(alpha > 0)) throw new Error("ridge: alpha must be positive");
  const width = checkInput(features, targets);
  const count = features.length;

  const means = columnMeans(features, width);
  const stds = means.map((mean, column) => {
    const variance =
      features.reduce((sum, row) => sum + ((row[column] as number) - mean) ** 2, 0) / count;
    const std = Math.sqrt(variance);
    return std === 0 ? 1 : std;
  });

  const expanded = features.map((row) => expand(standardise(row, means, stds), degree));
  const size = (expanded[0] as number[]).length;
  const expandedMeans = columnMeans(expanded, size);
  const centred = expanded.map((row) =>
    row.map((value, column) => value - (expandedMeans[column] as number)),
  );

  const outputs: Array<0 | 1> = [0, 1];
  const targetMeans = outputs.map((o) => targets.reduce((sum, t) => sum + t[o], 0) / count);

  // Normal equations (XcᵀXc + αI) w = Xcᵀ yc, symmetric, filled by its upper half.
  const gram: number[][] = Array.from({ length: size }, () => new Array<number>(size).fill(0));
  const moments: number[][] = outputs.map(() => new Array<number>(size).fill(0));
  centred.forEach((row, sample) => {
    for (let i = 0; i < size; i++) {
      const vi = row[i] as number;
      const gramRow = gram[i] as number[];
      for (let j = i; j < size; j++) gramRow[j] = (gramRow[j] as number) + vi * (row[j] as number);
      for (const o of outputs) {
        const moment = moments[o] as number[];
        moment[i] =
          (moment[i] as number) +
          vi * ((targets[sample] as [number, number])[o] - (targetMeans[o] as number));
      }
    }
  });
  for (let i = 0; i < size; i++) {
    const gramRow = gram[i] as number[];
    gramRow[i] = (gramRow[i] as number) + alpha;
    for (let j = 0; j < i; j++) gramRow[j] = (gram[j] as number[])[i] as number;
  }

  const solved = solveCholesky(gram, moments);
  const weights: [number[], number[]] = [solved[0] as number[], solved[1] as number[]];
  const intercept = (o: 0 | 1): number =>
    (targetMeans[o] as number) -
    expandedMeans.reduce(
      (sum, mean, column) => sum + mean * ((weights[o][column] as number) ?? 0),
      0,
    );
  return { alpha, degree, means, stds, weights, intercepts: [intercept(0), intercept(1)] };
}

/** Predicted [x, y] for one feature vector. */
export function predict(model: RidgeModel, features: readonly number[]): [number, number] {
  if (features.length !== model.means.length) {
    throw new Error(
      `ridge: the model takes ${model.means.length} features, got ${features.length}`,
    );
  }
  const expanded = expand(standardise(features, model.means, model.stds), model.degree);
  const dot = (weights: readonly number[], intercept: number): number =>
    expanded.reduce((sum, value, column) => sum + value * (weights[column] ?? 0), intercept);
  return [dot(model.weights[0], model.intercepts[0]), dot(model.weights[1], model.intercepts[1])];
}
