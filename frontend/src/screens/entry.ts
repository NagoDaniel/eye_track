// Entry screen (PLAN §5.2 step 1, §5.1): browser check, then the participant code.

import { ApiError } from "../api";
import { blockingMessage, missingRequirements } from "../browserSupport";
import { PARTICIPANT_CODE_PATTERN, type SessionCreateResponse, type StudyInfo } from "../config";
import { blockForever, card, el, p, render } from "../ui/dom";

export function normalizeParticipantCode(raw: string): string {
  return raw.trim().toUpperCase();
}

export function isValidParticipantCode(code: string): boolean {
  return PARTICIPANT_CODE_PATTERN.test(code);
}

export const INVALID_CODE_MESSAGE =
  "The code is two letters followed by four digits (month and day), for example AB0315.";

/** Inline message for a failed `POST /api/sessions`. */
export function sessionErrorMessage(error: ApiError): string {
  switch (error.status) {
    case 404:
      return "This study could not be found. Please check your link.";
    case 422:
      return INVALID_CODE_MESSAGE;
    case 429:
      return "Too many attempts from this network. Please wait a while and try again.";
    case 0:
      return "The server could not be reached. Please check your connection and try again.";
    default:
      return `The session could not be created (${error.message}).`;
  }
}

/** Resolves when the browser has everything the study needs; otherwise shows why and never resolves. */
export async function browserGate(
  root: HTMLElement,
  study: StudyInfo,
  missing: string[] = missingRequirements(study.browser_requirements),
): Promise<void> {
  if (missing.length === 0) return;
  render(root, card("Browser not supported", p(blockingMessage(missing), "error")));
  return blockForever();
}

/** Asks for the participant code; resolves with the created session. `prefill` comes from `?p=`. */
export function entryScreen(
  root: HTMLElement,
  study: StudyInfo,
  prefill: string | null,
  createSession: (participantCode: string) => Promise<SessionCreateResponse>,
): Promise<SessionCreateResponse> {
  return new Promise((resolve, reject) => {
    const input = el("input", { attrs: { type: "text", autocomplete: "off", maxlength: "6", id: "participant-code" } });
    input.value = normalizeParticipantCode(prefill ?? "");
    input.addEventListener("input", () => {
      const upper = input.value.toUpperCase();
      if (upper !== input.value) input.value = upper;
    });
    const message = p("", "error");
    const start = el("button", { text: "Continue", attrs: { type: "submit" } });
    const form = el(
      "form",
      {},
      el("label", {}, "Participant code ", input),
      message,
      start,
    );
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const code = normalizeParticipantCode(input.value);
      if (!isValidParticipantCode(code)) {
        message.textContent = INVALID_CODE_MESSAGE;
        return;
      }
      message.textContent = "";
      start.disabled = true;
      createSession(code).then(resolve, (error: unknown) => {
        if (error instanceof ApiError) {
          message.textContent = sessionErrorMessage(error);
          start.disabled = false;
        } else {
          reject(error);
        }
      });
    });
    render(root, card(study.name, p("Please enter your participant code."), form));
  });
}
