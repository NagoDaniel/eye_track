import { describe, expect, it } from "vitest";

import { blockingMessage, FEATURE_CHECKS, missingRequirements } from "./browserSupport";
import { BROWSER_REQUIREMENTS } from "./config";

describe("missingRequirements", () => {
  const checks = { a: () => true, b: () => false, c: () => true };

  it("lists the required APIs the browser lacks", () => {
    expect(missingRequirements(["a", "b", "c"], checks, [])).toEqual(["b"]);
    expect(missingRequirements(["a", "c"], checks, [])).toEqual([]);
  });

  it("treats a requirement without a check as missing (fail loudly)", () => {
    expect(missingRequirements(["a", "zzz"], checks, [])).toEqual(["zzz"]);
  });

  it("also checks what the app itself needs", () => {
    expect(missingRequirements(["a"], checks, ["b"])).toEqual(["b"]);
  });

  it("has a feature check for every requirement of the plan", () => {
    for (const name of BROWSER_REQUIREMENTS) expect(FEATURE_CHECKS[name]).toBeTypeOf("function");
  });
});

describe("blockingMessage", () => {
  it("names every missing API", () => {
    const message = blockingMessage(["MediaRecorder", "requestVideoFrameCallback"]);
    expect(message).toContain("MediaRecorder");
    expect(message).toContain("requestVideoFrameCallback");
    expect(message).toContain("cannot run the study");
  });
});
