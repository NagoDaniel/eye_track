// Preload screen (PLAN §5.2 step 3): progress bar; an integrity failure blocks with a clear message.

import type { VideoRef } from "../config";
import { IntegrityError, preloadVideos, type PreloadDeps } from "../preload";
import { button, card, el, p, render } from "../ui/dom";

export function preloadFailureMessage(error: unknown): string {
  if (error instanceof IntegrityError) {
    return (
      `Video ${error.videoId} was damaged during download (checksum mismatch), so the study cannot continue. ` +
      "Please try again; if it keeps failing, contact the researcher."
    );
  }
  const reason = error instanceof Error ? error.message : String(error);
  return `The videos could not be downloaded (${reason}). Please check your connection and try again.`;
}

/** Resolves with `video id -> object URL`. On failure the participant can only retry. */
export async function preloadScreen(
  root: HTMLElement,
  videos: readonly VideoRef[],
  deps: PreloadDeps = {},
): Promise<Map<string, string>> {
  for (;;) {
    const bar = el("progress", { attrs: { max: "1", value: "0" } });
    const label = p("Loading the videos ... 0 %", "muted");
    render(root, card("Preparing", p("The videos are being downloaded so that playback does not stall."), bar, label));
    try {
      return await preloadVideos(
        videos,
        (fraction) => {
          bar.value = fraction;
          label.textContent = `Loading the videos ... ${Math.round(fraction * 100)} %`;
        },
        deps,
      );
    } catch (error) {
      await new Promise<void>((retry) => {
        render(
          root,
          card("Cannot continue", p(preloadFailureMessage(error), "error"), button("Try again", retry)),
        );
      });
    }
  }
}
