// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api";
import type { SessionCreateResponse, StudyInfo } from "../config";
import { browserGate, entryScreen, isValidParticipantCode, normalizeParticipantCode } from "./entry";

const study: StudyInfo = {
  name: "Physics lecture study",
  code: "phys1",
  mode: "remote",
  consent_version: "v1",
  consent_text: "text",
  browser_requirements: ["getUserMedia", "MediaRecorder"],
};

const response = { session_id: "sid" } as SessionCreateResponse;

let root: HTMLElement;
beforeEach(() => {
  document.body.innerHTML = '<main id="app"></main>';
  root = document.getElementById("app") as HTMLElement;
});

function type(value: string): HTMLInputElement {
  const input = root.querySelector("input") as HTMLInputElement;
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  return input;
}

function submit(): void {
  (root.querySelector("form") as HTMLFormElement).dispatchEvent(new Event("submit", { cancelable: true }));
}

describe("participant code", () => {
  it("accepts two letters and a valid month/day, rejects the rest", () => {
    for (const ok of ["AB0315", "ZZ1231", "XY0101"]) expect(isValidParticipantCode(ok)).toBe(true);
    for (const bad of ["", "ab0315", "A10315", "AB1315", "AB0332", "AB031", "AB03155", "AB0300", "ÁB0315"]) {
      expect(isValidParticipantCode(bad)).toBe(false);
    }
    expect(normalizeParticipantCode("  ab0315 ")).toBe("AB0315");
  });
});

describe("entryScreen", () => {
  it("upper-cases what is typed and prefills from ?p=", () => {
    void entryScreen(root, study, "ab0315", vi.fn());
    const input = root.querySelector("input") as HTMLInputElement;
    expect(input.value).toBe("AB0315");
    expect(type("cd0229").value).toBe("CD0229");
  });

  it("rejects a bad code without calling the server", () => {
    const create = vi.fn();
    void entryScreen(root, study, null, create);
    type("ab13");
    submit();
    expect(create).not.toHaveBeenCalled();
    expect(root.textContent).toContain("two letters followed by four digits");
  });

  it("creates the session with the upper-cased code and resolves with it", async () => {
    const create = vi.fn().mockResolvedValue(response);
    const done = entryScreen(root, study, null, create);
    type("ab0315");
    submit();
    await expect(done).resolves.toBe(response);
    expect(create).toHaveBeenCalledWith("AB0315");
  });

  it("shows a server rejection inline and lets the participant try again", async () => {
    const create = vi.fn().mockRejectedValueOnce(new ApiError(429, "slow down")).mockResolvedValue(response);
    const done = entryScreen(root, study, null, create);
    type("ab0315");
    submit();
    await vi.waitFor(() => expect(root.textContent).toContain("Too many attempts"));
    expect((root.querySelector("button") as HTMLButtonElement).disabled).toBe(false);
    submit();
    await expect(done).resolves.toBe(response);
  });
});

describe("browserGate", () => {
  it("blocks with a message listing the missing APIs", () => {
    let resolved = false;
    void browserGate(root, study, ["MediaRecorder", "requestVideoFrameCallback"]).then(() => (resolved = true));
    expect(root.textContent).toContain("Browser not supported");
    expect(root.textContent).toContain("MediaRecorder, requestVideoFrameCallback");
    expect(resolved).toBe(false);
  });

  it("passes when nothing is missing", async () => {
    await browserGate(root, study, []);
    expect(root.textContent).toBe("");
  });
});
