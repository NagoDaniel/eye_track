// Landing page (the root URL, not in PLAN §5.2): asks for the study code and sends the student to
// that study's page (`/s/<code>`), where the session flow starts.

import { ApiError } from "../api";
import type { StudyInfo } from "../config";
import { STUDY_PATH_PREFIX } from "../constants";
import { blockForever, card, el, p, render } from "../ui/dom";

export const LANDING_HELP =
  "Enter the code of your study to start. Your teacher or researcher gave you this code; it is also the last part of the study link, after /s/ (for example phys1 in https://example.org/s/phys1). Type it exactly as you were given it.";
export const EMPTY_CODE_MESSAGE = "Please enter the study code.";

/** Inline message for a failed `GET /api/studies/{code}`. */
export function landingErrorMessage(error: ApiError): string {
  switch (error.status) {
    case 404:
      return "No study with this code was found. Please check the code and try again.";
    case 429:
      return "Too many attempts from this network. Please wait a while and try again.";
    case 0:
      return "The server could not be reached. Please check your connection and try again.";
    default:
      return `The study could not be looked up (${error.message}).`;
  }
}

/**
 * Shows the code box. A code that names a study goes to that study's page through `go` (the
 * server's spelling of the code is used); otherwise a message appears under the box.
 */
export function landingScreen(
  root: HTMLElement,
  lookup: (code: string) => Promise<StudyInfo>,
  go: (path: string) => void,
): Promise<never> {
  const input = el("input", {
    class: "study-code",
    attrs: { type: "text", autocomplete: "off", autocapitalize: "off", spellcheck: "false", id: "study-code" },
  });
  const message = p("", "error");
  const start = el("button", { text: "Continue", attrs: { type: "submit" } });
  const form = el("form", {}, el("label", {}, "Study code ", input), message, start);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const code = input.value.trim();
    if (code === "") {
      message.textContent = EMPTY_CODE_MESSAGE;
      return;
    }
    message.textContent = "";
    start.disabled = true;
    lookup(code).then(
      (study) => go(`${STUDY_PATH_PREFIX}${encodeURIComponent(study.code)}`),
      (error: unknown) => {
        message.textContent = error instanceof ApiError ? landingErrorMessage(error) : String(error);
        start.disabled = false;
      },
    );
  });
  render(root, card("Welcome", p(LANDING_HELP), form));
  input.focus();
  return blockForever();
}
