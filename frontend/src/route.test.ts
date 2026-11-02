import { describe, expect, it } from "vitest";

import { isLandingPath, parseRoute } from "./route";

describe("parseRoute", () => {
  it("reads the study code and the participant code", () => {
    expect(parseRoute("/s/phys1", "?p=AB0315")).toEqual({ studyCode: "phys1", participantCode: "AB0315" });
  });
  it("has no participant code without ?p=", () => {
    expect(parseRoute("/s/phys1/", "")).toEqual({ studyCode: "phys1", participantCode: null });
    expect(parseRoute("/s/phys1", "?p=").participantCode).toBeNull();
  });
  it("rejects other paths", () => {
    expect(() => parseRoute("/", "")).toThrow(/study code/);
    expect(() => parseRoute("/s/", "")).toThrow(/study code/);
    expect(() => parseRoute("/s/a/b", "")).toThrow(/study code/);
  });
});

describe("isLandingPath", () => {
  it("is the root only", () => {
    expect(isLandingPath("/")).toBe(true);
    expect(isLandingPath("")).toBe(true);
    expect(isLandingPath("/s/phys1")).toBe(false);
    expect(isLandingPath("/other")).toBe(false);
  });
});
