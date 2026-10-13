// Validation (PLAN §5.5): 5 points with the same dot behaviour. Shown but not scored in Milestone 1b.

import { assertPointCounts, validationPoints } from "../calibration/points";
import { runDotBlock } from "../calibration/dot";
import type { StudyConfig } from "../config";
import type { EventLog } from "../eventLog";

export function validationScreen(root: HTMLElement, config: StudyConfig, log: EventLog): Promise<void> {
  assertPointCounts(config);
  // The config has no separate validation dot duration: "same dot behaviour" (PLAN §5.5) means calibration's.
  return runDotBlock(
    root,
    "validation",
    validationPoints(window.innerWidth, window.innerHeight),
    config.calibration.dot_duration_ms,
    log,
  );
}
