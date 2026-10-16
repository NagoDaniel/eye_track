// Calibration with the quality gate and its retries (PLAN §5.6): calibration, the pre-video
// validation, the gate; on failure a short message and another attempt, up to `max_attempts`, then
// `on_fail` decides.

import type { StudyConfig } from "../config";
import type { CalibrationGate } from "../face/calibrationGate";
import type { GateReason } from "../eventLog";

/** The participant-facing cause (PLAN §5.6 "On failure"); never numbers. */
export const GATE_MESSAGES: Record<GateReason, string> = {
  ok: "",
  few_face_frames: "Keep your face in view of the camera.",
  low_brightness: "Add light in front of you.",
  head_or_distance_change: "Keep your head still and stay at the same distance from the screen.",
  high_error: "Let's try once more. Follow each dot closely with your eyes.",
  no_validation: "",
  forced_debug: "Let's try once more. Follow each dot closely with your eyes.",
};

export interface CalibrationFlowDeps {
  config: StudyConfig;
  gate: CalibrationGate;
  /** The clock of the event log (`performance.now()`). */
  now: () => number;
  calibrate: () => Promise<void>;
  validate: () => Promise<void>;
  /** Tell the participant why, and wait for them to press "Try again". */
  retry: (message: string) => Promise<void>;
  /** `end_session`: thank the participant and stop the study. */
  end: () => Promise<void>;
}

/** "continue": go on to the videos; "end": the study ends here (`on_fail: end_session`). */
export async function calibrationFlow(deps: CalibrationFlowDeps): Promise<"continue" | "end"> {
  const { config, gate } = deps;
  const maxAttempts = gate.enabled ? config.calibration_gate.max_attempts : 1;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    gate.beginAttempt(deps.now());
    await deps.calibrate();
    if (!config.validation.before_video) {
      gate.skipPreVideo(attempt);
      return "continue";
    }
    await deps.validate();
    const decision = gate.evaluatePreVideo(attempt);
    if (decision.passed) return "continue";
    if (attempt < maxAttempts) await deps.retry(GATE_MESSAGES[decision.reason]);
  }
  if (config.calibration_gate.on_fail === "end_session") {
    await deps.end();
    return "end";
  }
  return "continue"; // the server flags the session `calibration_gate_failed` from the events
}
