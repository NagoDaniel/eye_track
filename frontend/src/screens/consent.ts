// Consent screen (PLAN §5.2 step 2): nothing is recorded until the participant ticks the box and continues.

import { ApiError } from "../api";
import type { StudyInfo } from "../config";
import { card, el, p, render } from "../ui/dom";

/** `recordConsent` is `POST /api/sessions/{id}/consent`. Resolves once the server has the consent. */
export function consentScreen(
  root: HTMLElement,
  study: StudyInfo,
  recordConsent: () => Promise<unknown>,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const text = el("div", { class: "consent-text", text: study.consent_text });
    const box = el("input", { attrs: { type: "checkbox", id: "consent-box" } });
    const next = el("button", { text: "Continue", attrs: { type: "button" } });
    const message = p("", "error");
    next.disabled = true;
    box.addEventListener("change", () => {
      next.disabled = !box.checked;
    });
    next.addEventListener("click", () => {
      if (!box.checked) return; // the button is disabled; this guards against a forced click
      next.disabled = true;
      box.disabled = true;
      recordConsent().then(
        () => resolve(),
        (error: unknown) => {
          if (error instanceof ApiError) {
            message.textContent = `Your consent could not be saved (${error.message}). Please try again.`;
            next.disabled = false;
            box.disabled = false;
          } else {
            reject(error);
          }
        },
      );
    });
    render(
      root,
      card(
        "Consent",
        text,
        el("label", {}, box, " I have read the text above and agree to take part, including the recording of my webcam video."),
        message,
        next,
      ),
    );
  });
}
