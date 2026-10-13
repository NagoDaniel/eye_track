import { describe, expect, it } from "vitest";

import { parseRoute } from "./route";

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
