// Offered after a reload while an earlier session still has chunks in IndexedDB (PLAN §5.8).
// A reloaded session is ended, not continued: "resume" only uploads what is left.

import type { StoredSession } from "../upload/idb";
import { button, card, p, render } from "../ui/dom";

export type ResumeChoice = "resume" | "discard";

export function resumeScreen(
  root: HTMLElement,
  session: StoredSession,
  pendingChunks: number,
  problem: string | null = null,
): Promise<ResumeChoice> {
  return new Promise((resolve) => {
    const discard = button("Discard it", () => {
      if (discard.dataset["armed"] === "1") {
        resolve("discard");
        return;
      }
      discard.dataset["armed"] = "1";
      discard.textContent = "Click again to delete the recording data";
    });
    render(
      root,
      card(
        "Unfinished session",
        p(
          `A previous session (study ${session.studyCode}) was interrupted before everything was uploaded` +
            ` (${pendingChunks} part${pendingChunks === 1 ? "" : "s"} waiting). The recording cannot be continued,` +
            " but the data already collected can be uploaded.",
        ),
        ...(problem === null ? [] : [p(`The upload failed: ${problem}`, "error")]),
        button(problem === null ? "Resume upload" : "Try again", () => resolve("resume")),
        discard,
      ),
    );
  });
}
