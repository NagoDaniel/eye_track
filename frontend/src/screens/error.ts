// Error screen (PLAN §5.2): the end of the line for any unhandled failure.

import { ApiError } from "../api";
import { blockForever, card, p, render } from "../ui/dom";

/** Participant-facing text for a failure. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) return "The study could not be found. Please check your link.";
    if (error.status === 0) return "The server could not be reached. Please check your connection and reload.";
    return error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

/** Shows the failure and never resolves. `sessionId` helps the researcher find the session. */
export function errorScreen(
  root: HTMLElement,
  error: unknown,
  sessionId: string | null,
): Promise<never> {
  const children = [p(describeError(error), "error")];
  if (sessionId !== null) children.push(p(`Session: ${sessionId}`, "muted"));
  render(root, card("Something went wrong", ...children));
  return blockForever();
}
