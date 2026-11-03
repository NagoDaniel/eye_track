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
  it("accepts two letters, month, major letter and day, rejects the rest", () => {
    for (const ok of ["an03i05", "zz12s31", "xy01t01"]) expect(isValidParticipantCode(ok)).toBe(true);
    for (const bad of ["", "AN03I05", "a103i05", "an03x05", "an0305", "an03i5", "an03i055", "áb03i05"]) {
      expect(isValidParticipantCode(bad)).toBe(false);
    }
    expect(normalizeParticipantCode("  AN03I05 ")).toBe("an03i05");
  });
});

describe("entryScreen", () => {
  it("lower-cases what is typed and prefills from ?p=", () => {
    void entryScreen(root, study, "AN03I05", vi.fn());
    const input = root.querySelector("input") as HTMLInputElement;
    expect(input.value).toBe("an03i05");
    expect(type("CD02G29").value).toBe("cd02g29");
  });

  it("rejects a bad code without calling the server", () => {
    const create = vi.fn();
    void entryScreen(root, study, null, create);
    type("ab13");
    submit();
    expect(create).not.toHaveBeenCalled();
    expect(root.textContent).toContain("the letter of your major");
  });

  it("creates the session with the lower-cased code and resolves with it", async () => {
    const create = vi.fn().mockResolvedValue(response);
    const done = entryScreen(root, study, null, create);
    type("AN03I05");
    submit();
    await expect(done).resolves.toBe(response);
    expect(create).toHaveBeenCalledWith("an03i05");
  });

  it("shows a server rejection inline and lets the participant try again", async () => {
    const create = vi.fn().mockRejectedValueOnce(new ApiError(429, "slow down")).mockResolvedValue(response);
    const done = entryScreen(root, study, null, create);
    type("AN03I05");
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
