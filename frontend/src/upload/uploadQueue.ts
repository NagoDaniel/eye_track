// Upload queue (PLAN §5.8). Every chunk is written to IndexedDB first, then uploaded sequentially
// per stream with exponential backoff; it is deleted from IndexedDB only after a 2xx.

import { ApiError, type Api, sha256Hex } from "../api";
import {
  COMPLETE_MAX_ROUNDS,
  UPLOAD_BACKOFF_BASE_MS,
  UPLOAD_BACKOFF_FACTOR,
  UPLOAD_BACKOFF_MAX_MS,
} from "../constants";
import type { CompleteRequest } from "../config";
import { STREAMS, type Stream } from "../eventLog";
import type { StoredChunk, StoredSession, UploadDb } from "./idb";

export interface QueueStats {
  pendingChunks: number;
  pendingBytes: number;
  /** The loud failure that stopped the queue, if any (409, 401, ...). */
  error: Error | null;
}

export interface UploadQueueOptions {
  sleep?: (ms: number) => Promise<void>;
  onError?: (error: Error) => void;
}

/** Delay before retry number `attempt` (0-based): base * factor^attempt, capped. */
export function backoffDelay(attempt: number): number {
  return Math.min(UPLOAD_BACKOFF_BASE_MS * UPLOAD_BACKOFF_FACTOR ** attempt, UPLOAD_BACKOFF_MAX_MS);
}

const defaultSleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

interface Waiter {
  resolve: () => void;
  reject: (error: Error) => void;
}

export class UploadQueue {
  private readonly pending = new Map<Stream, StoredChunk[]>();
  private readonly running = new Set<Stream>();
  private readonly listeners = new Set<(stats: QueueStats) => void>();
  private readonly waiters: Waiter[] = [];
  private enqueueChain: Promise<unknown> = Promise.resolve();
  private fatal: Error | null = null;
  private readonly sleep: (ms: number) => Promise<void>;

  private constructor(
    private readonly db: UploadDb,
    private readonly api: Pick<Api, "putChunk" | "complete">,
    private readonly state: StoredSession,
    private readonly options: UploadQueueOptions,
  ) {
    this.sleep = options.sleep ?? defaultSleep;
  }

  /** Start (or, after a reload, resume) a session's queue: stored chunks go back into the queue. */
  static async open(
    db: UploadDb,
    api: Pick<Api, "putChunk" | "complete">,
    session: StoredSession,
    options: UploadQueueOptions = {},
  ): Promise<UploadQueue> {
    const queue = new UploadQueue(db, api, structuredClone(session), options);
    await db.putSession(queue.state);
    for (const chunk of await db.chunksOf(session.sessionId)) queue.addPending(chunk);
    for (const stream of STREAMS) queue.kick(stream);
    return queue;
  }

  get sessionId(): string {
    return this.state.sessionId;
  }

  stats(): QueueStats {
    let pendingChunks = 0;
    let pendingBytes = 0;
    for (const chunks of this.pending.values()) {
      pendingChunks += chunks.length;
      for (const c of chunks) pendingBytes += c.bytes.byteLength;
    }
    return { pendingChunks, pendingBytes, error: this.fatal };
  }

  subscribe(listener: (stats: QueueStats) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Store `bytes` as the next chunk of `stream` and start uploading it. Returns its seq. */
  enqueue(stream: Stream, bytes: Uint8Array): Promise<number> {
    const result = this.enqueueChain.then(() => this.store(stream, bytes));
    this.enqueueChain = result.catch(() => undefined);
    return result;
  }

  /** Resolves when nothing is pending; rejects with the queue's fatal error. */
  idle(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.waiters.push({ resolve, reject });
      this.settle();
    });
  }

  /**
   * Upload what is pending, then call /complete and re-send any chunks the server lacks until it
   * reports `uploaded`. `interrupted` is true only for a session resumed after a reload.
   */
  async finish(interrupted: boolean): Promise<void> {
    for (let round = 0; round < COMPLETE_MAX_ROUNDS; round++) {
      await this.enqueueChain;
      await this.idle();
      const request: CompleteRequest = { last_seq: this.lastSeq(), interrupted };
      const response = await this.retrying(() =>
        this.api.complete(this.state.sessionId, this.state.uploadToken, request),
      );
      if (response.status === "uploaded") {
        await this.db.deleteSession(this.state.sessionId);
        return;
      }
      let requeued = 0;
      for (const stream of STREAMS) {
        for (const seq of response.missing[stream] ?? []) {
          const chunk = await this.db.getChunk(this.state.sessionId, stream, seq);
          if (chunk === undefined) {
            throw this.fail(
              new Error(`The server is missing ${stream} chunk ${seq}, which is no longer stored locally`),
            );
          }
          this.addPending(chunk);
          requeued++;
        }
      }
      if (requeued === 0) {
        throw this.fail(new Error("The server reports the upload incomplete but names no missing chunk"));
      }
      for (const stream of STREAMS) this.kick(stream);
    }
    throw this.fail(new Error(`The upload is still incomplete after ${COMPLETE_MAX_ROUNDS} rounds`));
  }

  private lastSeq(): Partial<Record<Stream, number>> {
    const last: Partial<Record<Stream, number>> = {};
    for (const stream of STREAMS) {
      const next = this.state.nextSeq[stream] ?? 0;
      if (next > 0) last[stream] = next - 1;
    }
    return last;
  }

  private async store(stream: Stream, bytes: Uint8Array): Promise<number> {
    if (this.fatal !== null) throw this.fatal;
    const seq = this.state.nextSeq[stream] ?? 0;
    const chunk: StoredChunk = {
      sessionId: this.state.sessionId,
      stream,
      seq,
      sha256: await sha256Hex(bytes),
      bytes,
    };
    const updated: StoredSession = { ...this.state, nextSeq: { ...this.state.nextSeq, [stream]: seq + 1 } };
    try {
      await this.db.putChunk(chunk, updated);
    } catch (e) {
      // Cannot persist (storage full or blocked): data would be lost silently, so stop loudly.
      throw this.fail(e instanceof Error ? e : new Error(String(e)));
    }
    this.state.nextSeq[stream] = seq + 1;
    this.addPending(chunk);
    this.kick(stream);
    return seq;
  }

  private addPending(chunk: StoredChunk): void {
    const list = this.pending.get(chunk.stream) ?? [];
    if (!list.some((c) => c.seq === chunk.seq)) list.push(chunk);
    list.sort((a, b) => a.seq - b.seq);
    this.pending.set(chunk.stream, list);
    this.emit();
  }

  private kick(stream: Stream): void {
    if (this.running.has(stream) || this.fatal !== null) return;
    if ((this.pending.get(stream)?.length ?? 0) === 0) return;
    this.running.add(stream);
    void this.pump(stream);
  }

  private async pump(stream: Stream): Promise<void> {
    try {
      for (;;) {
        const chunk = this.pending.get(stream)?.[0];
        if (chunk === undefined || this.fatal !== null) break;
        await this.retrying(() =>
          this.api.putChunk(chunk.sessionId, this.state.uploadToken, stream, chunk.seq, chunk.bytes, chunk.sha256),
        );
        await this.db.deleteChunk(chunk);
        this.pending.get(stream)?.shift();
        this.emit();
      }
    } catch (e) {
      this.fail(e instanceof Error ? e : new Error(String(e)));
    } finally {
      this.running.delete(stream);
      this.settle();
    }
  }

  /** Retry 5xx and network errors with backoff; anything else is thrown (409 is never retried blindly). */
  private async retrying<T>(attempt: () => Promise<T>): Promise<T> {
    for (let n = 0; ; n++) {
      try {
        return await attempt();
      } catch (e) {
        if (!(e instanceof ApiError) || !e.retryable) throw e;
        await this.sleep(backoffDelay(n));
      }
    }
  }

  private fail(error: Error): Error {
    if (this.fatal === null) {
      this.fatal = error;
      this.options.onError?.(error);
    }
    this.emit();
    this.settle();
    return error;
  }

  private settle(): void {
    if (this.fatal !== null) {
      for (const w of this.waiters.splice(0)) w.reject(this.fatal);
    } else if (this.running.size === 0 && this.stats().pendingChunks === 0) {
      for (const w of this.waiters.splice(0)) w.resolve();
    }
  }

  private emit(): void {
    const stats = this.stats();
    for (const listener of this.listeners) listener(stats);
  }
}
