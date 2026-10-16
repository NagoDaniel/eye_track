// Calibration (PLAN §5.5): 13 dots, centre first, the rest in random order.

import { assertPointCounts, calibrationPoints } from "../calibration/points";
import { runDotBlock } from "../calibration/dot";
import type { StudyConfig } from "../config";
import type { EventLog } from "../eventLog";

export function calibrationScreen(
  root: HTMLElement,
  config: StudyConfig,
  log: EventLog,
  random: () => number = Math.random,
): Promise<void> {
  assertPointCounts(config);
  const points = calibrationPoints(
    window.innerWidth,
    window.innerHeight,
    config.calibration.margin,
    random,
  );
  return runDotBlock(root, "calibration", points, config.calibration.dot_duration_ms, log, {
    fullscreen: { required: config.playback.require_fullscreen },
  });
}
