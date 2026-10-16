import { describe, expect, it, vi } from "vitest";

import type { Stream } from "../eventLog";
import { JsonlUploader } from "./jsonlUploader";

interface Sent {
  stream: Stream;
  lines: unknown[];
}

async function gunzip(bytes: Uint8Array): Promise<string> {
  const source = new Blob([bytes as BlobPart]).stream();
  return new Response(source.pipeThrough(new DecompressionStream("gzip"))).text();
}

function fakeQueue(): { sent: Sent[]; enqueue: (s: Stream, b: Uint8Array) => Promise<number> } {
  const sent: Sent[] = [];
  return {
    sent,
    enqueue: async (stream, bytes) => {
      const text = await gunzip(bytes);
      const lines = text
        .trimEnd()
        .split("\n")
        .map((line): unknown => JSON.parse(line));
      sent.push({ stream, lines });
      return sent.length - 1;
    },
  };
}

describe("JsonlUploader", () => {
  it("flush sends the buffered records as one gzipped JSONL chunk to its own stream", async () => {
    const queue = fakeQueue();
    const uploader = new JsonlUploader<{ n: number }>(queue, "features", 2000, () => undefined);
    uploader.push({ n: 1 });
    uploader.push({ n: 2 });
    expect(uploader.hasUnsent()).toBe(true);
    await uploader.flush();
    uploader.push({ n: 3 });
    await uploader.flush();
    expect(uploader.hasUnsent()).toBe(false);
    expect(queue.sent).toEqual([
      { stream: "features", lines: [{ n: 1 }, { n: 2 }] },
      { stream: "features", lines: [{ n: 3 }] },
    ]);
  });

  it("makes no chunk for an empty interval", async () => {
    const queue = fakeQueue();
    const uploader = new JsonlUploader<number>(queue, "stimulus_frames", 10, () => undefined);
    uploader.start();
    await new Promise((resolve) => setTimeout(resolve, 60));
    await uploader.stop();
    expect(queue.sent).toEqual([]);
  });

  it("the timer sends a chunk every interval, stop sends the rest and ends the timer", async () => {
    const queue = fakeQueue();
    const uploader = new JsonlUploader<number>(queue, "events", 20, () => undefined);
    uploader.start();
    uploader.push(1);
    await vi.waitFor(() => expect(queue.sent).toHaveLength(1));
    uploader.push(2);
    await uploader.stop();
    expect(queue.sent).toEqual([
      { stream: "events", lines: [1] },
      { stream: "events", lines: [2] },
    ]);
    uploader.push(3);
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(queue.sent).toHaveLength(2); // the timer is gone
  });
});
