// Turns EventLog records into gzipped JSON-line chunks, one per `chunk_ms` interval (PLAN §5.9).
// An empty interval makes no chunk, so seqs stay contiguous.

import type { EventRecord } from "../eventLog";
import { gzip } from "./gzip";
import type { UploadQueue } from "./uploadQueue";

export class EventsUploader {
  private buffer: EventRecord[] = [];
  private timer: ReturnType<typeof setInterval> | null = null;
  private chain: Promise<unknown> = Promise.resolve();

  constructor(
    private readonly queue: Pick<UploadQueue, "enqueue">,
    private readonly chunkMs: number,
    private readonly onError: (error: Error) => void,
  ) {}

  /** The EventLog sink. */
  readonly push = (record: EventRecord): void => {
    this.buffer.push(record);
  };

  /** True while records wait for the next chunk or a chunk is being written. */
  hasUnsent(): boolean {
    return this.buffer.length > 0 || this.timer !== null;
  }

  start(): void {
    if (this.timer !== null) return;
    this.timer = setInterval(() => {
      this.flush().catch((e: unknown) => this.onError(e instanceof Error ? e : new Error(String(e))));
    }, this.chunkMs);
  }

  /** Stop the timer and send what is buffered. */
  async stop(): Promise<void> {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    await this.flush();
  }

  /** Send the buffered records as one chunk (none if empty). Chunks are written in call order. */
  flush(): Promise<void> {
    const records = this.buffer;
    this.buffer = [];
    const run = async (): Promise<void> => {
      if (records.length === 0) return;
      const text = records.map((r) => JSON.stringify(r)).join("\n") + "\n";
      await this.queue.enqueue("events", await gzip(text));
    };
    const result = this.chain.catch(() => undefined).then(run);
    this.chain = result;
    return result;
  }
}
