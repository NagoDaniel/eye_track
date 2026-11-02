// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api";
import type { StudyInfo } from "../config";
import { EMPTY_CODE_MESSAGE, LANDING_HELP, landingScreen } from "./landing";

const study: StudyInfo = {
  name: "Physics lecture study",
  code: "phys1",
  mode: "remote",
  consent_version: "v1",
  consent_text: "text",
  browser_requirements: [],
};

let root: HTMLElement;
beforeEach(() => {
  document.body.innerHTML = '<main id="app"></main>';
  root = document.getElementById("app") as HTMLElement;
});

const input = (): HTMLInputElement => root.querySelector("input") as HTMLInputElement;
const message = (): string => (root.querySelector("p.error") as HTMLElement).textContent ?? "";
const button = (): HTMLButtonElement => root.querySelector("button") as HTMLButtonElement;

function submit(value: string): void {
  input().value = value;
  (root.querySelector("form") as HTMLFormElement).dispatchEvent(new Event("submit", { cancelable: true }));
}
const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

describe("landingScreen", () => {
  it("explains what to enter", () => {
    void landingScreen(root, vi.fn(), vi.fn());
    expect(root.textContent).toContain(LANDING_HELP);
    expect(root.querySelector("label")?.textContent).toContain("Study code");
  });

  it("goes to the study page of a valid code, in the server's spelling", async () => {
    const lookup = vi.fn().mockResolvedValue(study);
    const go = vi.fn();
    void landingScreen(root, lookup, go);
    submit("  phys1 ");
    await settle();
    expect(lookup).toHaveBeenCalledWith("phys1");
    expect(go).toHaveBeenCalledWith("/s/phys1");
  });

  it("keeps the case of the code it sends", async () => {
    const lookup = vi.fn().mockResolvedValue({ ...study, code: "Phys 1" });
    const go = vi.fn();
    void landingScreen(root, lookup, go);
    submit("Phys 1");
    await settle();
    expect(go).toHaveBeenCalledWith("/s/Phys%201");
  });

  it("does not look up an empty code", () => {
    const lookup = vi.fn();
    void landingScreen(root, lookup, vi.fn());
    submit("   ");
    expect(lookup).not.toHaveBeenCalled();
    expect(message()).toBe(EMPTY_CODE_MESSAGE);
  });

  it("says when no study has the code, and lets the student try again", async () => {
    const go = vi.fn();
    void landingScreen(root, vi.fn().mockRejectedValue(new ApiError(404, "unknown study")), go);
    submit("nope");
    expect(button().disabled).toBe(true);
    await settle();
    expect(message()).toContain("No study with this code");
    expect(button().disabled).toBe(false);
    expect(go).not.toHaveBeenCalled();
  });

  it("tells about rate limits and an unreachable server", async () => {
    for (const [status, text] of [[429, "Too many attempts"], [0, "could not be reached"]] as const) {
      void landingScreen(root, vi.fn().mockRejectedValue(new ApiError(status, "x")), vi.fn());
      submit("a");
      await settle();
      expect(message()).toContain(text);
    }
  });
});
