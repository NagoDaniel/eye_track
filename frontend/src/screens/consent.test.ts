// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api";
import type { StudyInfo } from "../config";
import { consentScreen } from "./consent";

const study: StudyInfo = {
  name: "S",
  code: "s",
  mode: "remote",
  consent_version: "v1",
  consent_text: "Your webcam video is recorded <b>and stored</b>.",
  browser_requirements: [],
};

let root: HTMLElement;
beforeEach(() => {
  document.body.innerHTML = '<main id="app"></main>';
  root = document.getElementById("app") as HTMLElement;
});

const box = (): HTMLInputElement => root.querySelector('input[type="checkbox"]') as HTMLInputElement;
const next = (): HTMLButtonElement => root.querySelector("button") as HTMLButtonElement;
function tick(): void {
  box().checked = true;
  box().dispatchEvent(new Event("change"));
}

describe("consentScreen", () => {
  it("shows the consent text as text, not as HTML", () => {
    void consentScreen(root, study, vi.fn());
    expect(root.querySelector(".consent-text")?.textContent).toBe(study.consent_text);
    expect(root.querySelector(".consent-text b")).toBeNull();
  });

  it("keeps the continue button disabled until the box is ticked, and does nothing when forced", () => {
    const record = vi.fn().mockResolvedValue(undefined);
    void consentScreen(root, study, record);
    expect(next().disabled).toBe(true);
    next().click();
    expect(record).not.toHaveBeenCalled();
    tick();
    expect(next().disabled).toBe(false);
    box().checked = false;
    box().dispatchEvent(new Event("change"));
    expect(next().disabled).toBe(true);
  });

  it("records consent and resolves after the tick box and click", async () => {
    const record = vi.fn().mockResolvedValue(undefined);
    const done = consentScreen(root, study, record);
    tick();
    next().click();
    await done;
    expect(record).toHaveBeenCalledTimes(1);
  });

  it("stays on the screen when the server refuses", async () => {
    const record = vi.fn().mockRejectedValueOnce(new ApiError(500, "oops")).mockResolvedValue(undefined);
    const done = consentScreen(root, study, record);
    tick();
    next().click();
    await vi.waitFor(() => expect(root.textContent).toContain("could not be saved"));
    expect(next().disabled).toBe(false);
    next().click();
    await done;
  });
});
