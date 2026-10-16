// Upload screen (PLAN §5.2 step 11): flush the events, finish the upload, show progress.
// The unload guard (installed for the whole session) warns while this runs.

import type { UploadQueue } from "../upload/uploadQueue";
import { card, el, p, render } from "../ui/dom";

export function uploadProgress(total: number, pending: number): number {
  return total === 0 ? 1 : Math.min(Math.max(1 - pending / total, 0), 1);
}

/** Anything with buffered records that must be sent before the upload is finished. */
export interface Flushable {
  stop(): Promise<void>;
}

/**
 * `uploaders` are flushed in the given order (empty for a resumed session: its in-memory records
 * were lost with the page); `interrupted` is true only then. Rejects with the queue's loud failure
 * (e.g. a 409).
 */
export async function uploadScreen(
  root: HTMLElement,
  queue: UploadQueue,
  options: { uploaders: readonly Flushable[]; interrupted: boolean },
): Promise<void> {
  const bar = el("progress", { attrs: { max: "1", value: "0" } });
  const label = p("Uploading ...", "muted");
  render(
    root,
    card("Uploading your recording", p("Please keep this page open until the upload has finished."), bar, label),
  );
  for (const uploader of options.uploaders) await uploader.stop();
  const total = Math.max(queue.stats().pendingChunks, 1);
  const unsubscribe = queue.subscribe((stats) => {
    bar.value = uploadProgress(total, stats.pendingChunks);
    label.textContent = `${stats.pendingChunks} part${stats.pendingChunks === 1 ? "" : "s"} left (${Math.ceil(stats.pendingBytes / 1024)} KB)`;
  });
  try {
    await queue.finish(options.interrupted);
  } finally {
    unsubscribe();
  }
  bar.value = 1;
}
