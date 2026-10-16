import { describe, expect, it } from "vitest";

import { sessionPlan, type Step } from "./sessionPlan";

const validation = (before: boolean, between: boolean, after: boolean) => ({
  before_video: before,
  between_videos: between,
  after_last_video: after,
  points: 5,
});
const kinds = (steps: Step[]): string[] =>
  steps.map((s) =>
    s.kind === "playback"
      ? `playback:${s.videoId}`
      : s.kind === "validation"
        ? `validation:${s.block}`
        : s.kind,
  );

describe("sessionPlan", () => {
  it("runs fullscreen, calibration (with its validation), each video with a validation between, a final validation, upload, done", () => {
    const plan = sessionPlan({
      playlist: ["1", "2", "3"],
      validation: validation(true, true, true),
    });
    expect(kinds(plan)).toEqual([
      "fullscreen",
      "calibration",
      "playback:1",
      "validation:between_videos",
      "playback:2",
      "validation:between_videos",
      "playback:3",
      "validation:after_last_video",
      "upload",
      "done",
    ]);
  });

  it("leaves out the validations the study switches off", () => {
    expect(
      kinds(sessionPlan({ playlist: ["1", "2"], validation: validation(false, false, false) })),
    ).toEqual(["fullscreen", "calibration", "playback:1", "playback:2", "upload", "done"]);
    expect(
      kinds(sessionPlan({ playlist: ["1", "2"], validation: validation(true, false, true) })),
    ).toEqual([
      "fullscreen",
      "calibration",
      "playback:1",
      "playback:2",
      "validation:after_last_video",
      "upload",
      "done",
    ]);
  });

  it("has no validation between videos after the last one (only the final one)", () => {
    expect(
      kinds(sessionPlan({ playlist: ["1"], validation: validation(true, true, false) })),
    ).toEqual(["fullscreen", "calibration", "playback:1", "upload", "done"]);
  });
});
