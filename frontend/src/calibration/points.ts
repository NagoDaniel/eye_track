// Pure geometry for calibration, validation and playback (PLAN §5.5, §5.7).

import { CALIBRATION_POINT_COUNT, INNER_POINT_HIGH, INNER_POINT_LOW, VALIDATION_POINT_COUNT } from "../constants";
import type { StudyConfig } from "../config";
import type { Rect, Size } from "../eventLog";

export interface Point {
  x: number;
  y: number;
}

/** Fail loudly if the study asks for other point counts than the plan's fixed 13 and 5. */
export function assertPointCounts(config: Pick<StudyConfig, "calibration" | "validation">): void {
  if (config.calibration.points !== CALIBRATION_POINT_COUNT) {
    throw new Error(`calibration.points is ${config.calibration.points}; this app supports exactly ${CALIBRATION_POINT_COUNT}`);
  }
  if (config.validation.points !== VALIDATION_POINT_COUNT) {
    throw new Error(`validation.points is ${config.validation.points}; this app supports exactly ${VALIDATION_POINT_COUNT}`);
  }
}

/**
 * 13 calibration points in viewport CSS px: a 3x3 grid (columns and rows at margin, 0.5, 1 - margin)
 * plus the 4 quadrant centres (0.25 / 0.75). The centre comes first, the other 12 in random order.
 * `random` returns a number in [0, 1) and is injectable for tests.
 */
export function calibrationPoints(
  viewportW: number,
  viewportH: number,
  margin: number,
  random: () => number = Math.random,
): Point[] {
  if (!(margin > 0 && margin < INNER_POINT_LOW)) {
    throw new Error(`calibration margin ${margin} must be between 0 and ${INNER_POINT_LOW}`);
  }
  const edges = [margin, 0.5, 1 - margin];
  const quadrants = [INNER_POINT_LOW, INNER_POINT_HIGH];
  const fractions: [number, number][] = [];
  for (const fy of edges) for (const fx of edges) if (!(fx === 0.5 && fy === 0.5)) fractions.push([fx, fy]);
  for (const fy of quadrants) for (const fx of quadrants) fractions.push([fx, fy]);
  // Fisher-Yates
  for (let i = fractions.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    const a = fractions[i] as [number, number];
    fractions[i] = fractions[j] as [number, number];
    fractions[j] = a;
  }
  return [[0.5, 0.5] as [number, number], ...fractions].map(([fx, fy]) => ({ x: fx * viewportW, y: fy * viewportH }));
}

/** 5 validation points: the centre and the 4 inner corners at 25 % / 75 %. Never used for fitting. */
export function validationPoints(viewportW: number, viewportH: number): Point[] {
  const fractions: [number, number][] = [
    [0.5, 0.5],
    [INNER_POINT_LOW, INNER_POINT_LOW],
    [INNER_POINT_HIGH, INNER_POINT_LOW],
    [INNER_POINT_LOW, INNER_POINT_HIGH],
    [INNER_POINT_HIGH, INNER_POINT_HIGH],
  ];
  return fractions.map(([fx, fy]) => ({ x: fx * viewportW, y: fy * viewportH }));
}

/**
 * Where the video's pixels are on screen for `object-fit: contain`, from the intrinsic size
 * (not from the element box), in viewport CSS px.
 */
export function contentRect(viewport: Size, intrinsicW: number, intrinsicH: number): Rect {
  if (!(intrinsicW > 0 && intrinsicH > 0)) throw new Error(`video has no intrinsic size (${intrinsicW}x${intrinsicH})`);
  const scale = Math.min(viewport.width / intrinsicW, viewport.height / intrinsicH);
  const width = intrinsicW * scale;
  const height = intrinsicH * scale;
  return { x: (viewport.width - width) / 2, y: (viewport.height - height) / 2, width, height };
}
