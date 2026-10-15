import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, sha256Hex } from "../api";
import type { ChunkAck, CompleteRequest, CompleteResponse } from "../config";
import { UPLOAD_BACKOFF_BASE_MS, UPLOAD_BACKOFF_MAX_MS } from "../constants";
import { EventLog, type EventRecord, type Stream } from "../eventLog";
import { EventsUploader } from "./eventsUploader";
import { UploadDb, type StoredSession } from "./idb";
import { backoffDelay, UploadQueue } from "./uploadQueue";

interface Put {
  stream: Stream;
  seq: number;
  sha256: string;
  bytes: Uint8Array;
  token: string;
}

type PutHandler = (put: Put, attempt: number) => Promise<ChunkAck> | ChunkAck;
type CompleteHandler = (request: CompleteRequest, call: number) => CompleteResponse;

class StubApi {
  puts: Put[] = [];
  completes: CompleteRequest[] = [];
  private attempts = new Map<string, number>();
  constructor(
    public onPut: PutHandler = (p) => ({ stream: p.stream, seq: p.seq, size: p.bytes.length }),
    public onComplete: CompleteHandler = () => ({ status: "uploaded", missing: {} }),
  ) {}

  async putChunk(
    _id: string,
    token: string,
    stream: Stream,
    seq: number,
    bytes: Uint8Array,
    sha256: string,
  ): Promise<ChunkAck> {
    const put = { stream, seq, sha256, bytes, token };
    this.puts.push(put);
    const key = `${stream}/${seq}`;
    const attempt = this.attempts.get(key) ?? 0;
    this.attempts.set(key, attempt + 1);
    return this.onPut(put, attempt);
  }

  async complete(_id: string, _token: string, request: CompleteRequest): Promise<CompleteResponse> {
    this.completes.push(request);
    return this.onComplete(request, this.completes.length - 1);
  }
}

const SESSION: StoredSession = { sessionId: "sid", uploadToken: "tok", studyCode: "phys1", nextSeq: {} };
const bytes = (...values: number[]): Uint8Array => new Uint8Array(values);

let factory: IDBFactory;
let db: UploadDb;

beforeEach(async () => {
  factory = new IDBFactory();
  db = await UploadDb.open(factory);
});

afterEach(() => {
  vi.useRealTimers();
});

async function open(api: StubApi, onError?: (e: Error) => void): Promise<UploadQueue> {
  return UploadQueue.open(db, api, SESSION, onError ? { onError } : {});
}

describe("backoffDelay", () => {
  it("doubles from the base and is capped at the 30 s constant", () => {
    expect(backoffDelay(0)).toBe(UPLOAD_BACKOFF_BASE_MS);
    expect(backoffDelay(1)).toBe(UPLOAD_BACKOFF_BASE_MS * 2);
    expect(backoffDelay(50)).toBe(UPLOAD_BACKOFF_MAX_MS);
    expect(UPLOAD_BACKOFF_MAX_MS).toBe(30_000);
  });
});

describe("UploadQueue", () => {
  it("uploads each stream in order with token and hash, and empties IndexedDB", async () => {
    const api = new StubApi();
    const queue = await open(api);
    for (let i = 0; i < 4; i++) await queue.enqueue("events", bytes(i, i));
    await queue.enqueue("webcam", bytes(9));
    await queue.idle();

    expect(api.puts.filter((p) => p.stream === "events").map((p) => p.seq)).toEqual([0, 1, 2, 3]);
    expect(api.puts.filter((p) => p.stream === "webcam").map((p) => p.seq)).toEqual([0]);
    const first = api.puts[0];
    expect(first?.token).toBe("tok");
    expect(first?.sha256).toBe(await sha256Hex(first?.bytes ?? bytes()));
    expect(await db.chunksOf("sid")).toEqual([]);
  });

  it("writes a chunk to IndexedDB before uploading it and keeps it until the 2xx", async () => {
    let release: (ack: ChunkAck) => void = () => undefined;
    const gate = new Promise<ChunkAck>((resolve) => (release = resolve));
    const api = new StubApi(() => gate);
    const queue = await open(api);
    await queue.enqueue("events", bytes(1));
    await vi.waitFor(() => expect(api.puts).toHaveLength(1));
    expect((await db.chunksOf("sid")).map((c) => c.seq)).toEqual([0]);
    release({ stream: "events", seq: 0, size: 1 });
    await queue.idle();
    expect(await db.chunksOf("sid")).toEqual([]);
  });

  it("retries a 5xx and a network error with exponential backoff", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const api = new StubApi((p, attempt) => {
      if (attempt === 0) throw new ApiError(503, "busy");
      if (attempt === 1) throw new ApiError(0, "Failed to fetch");
      return { stream: p.stream, seq: p.seq, size: p.bytes.length };
    });
    const queue = await open(api);
    await queue.enqueue("events", bytes(1));

    await vi.advanceTimersByTimeAsync(0);
    expect(api.puts).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(UPLOAD_BACKOFF_BASE_MS - 1);
    expect(api.puts).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.puts).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(backoffDelay(1) - 1);
    expect(api.puts).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.puts).toHaveLength(3);
    await queue.idle();
    expect(await db.chunksOf("sid")).toEqual([]);
  });

  it("does not move on to the next chunk of a stream while one is being retried", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const api = new StubApi((p, attempt) => {
      if (p.seq === 0 && attempt === 0) throw new ApiError(500, "boom");
      return { stream: p.stream, seq: p.seq, size: 1 };
    });
    const queue = await open(api);
    await queue.enqueue("events", bytes(1));
    await queue.enqueue("events", bytes(2));
    await vi.advanceTimersByTimeAsync(UPLOAD_BACKOFF_BASE_MS);
    await queue.idle();
    expect(api.puts.map((p) => p.seq)).toEqual([0, 0, 1]);
  });

  it("surfaces a 409 as an error and does not retry it", async () => {
    const errors: Error[] = [];
    const api = new StubApi(() => {
      throw new ApiError(409, "chunk differs");
    });
    const queue = await open(api, (e) => errors.push(e));
    await queue.enqueue("events", bytes(1));
    await expect(queue.idle()).rejects.toMatchObject({ status: 409 });
    expect(api.puts).toHaveLength(1);
    expect(errors).toHaveLength(1);
    expect(queue.stats().error).toBe(errors[0]);
    await expect(queue.enqueue("events", bytes(2))).rejects.toMatchObject({ status: 409 });
    await expect(queue.finish(false)).rejects.toMatchObject({ status: 409 });
    expect(api.completes).toHaveLength(0);
    expect(await db.chunksOf("sid")).toHaveLength(1); // kept, not lost
  });

  it("survives a reload: a new queue on the same database finishes the upload as interrupted", async () => {
    const stuck = new StubApi(() => new Promise<ChunkAck>(() => undefined)); // the page "dies" mid-upload
    const before = await open(stuck);
    await before.enqueue("events", bytes(1));
    await before.enqueue("events", bytes(2));
    await before.enqueue("webcam", bytes(3));
    db.close();

    db = await UploadDb.open(factory); // same fake database, new page
    const stored = await db.listSessions();
    expect(stored).toHaveLength(1);
    expect(stored[0]).toMatchObject({ sessionId: "sid", uploadToken: "tok", studyCode: "phys1" });
    expect(stored[0]?.nextSeq).toEqual({ events: 2, webcam: 1 });

    const api = new StubApi();
    const queue = await UploadQueue.open(db, api, stored[0] as StoredSession);
    await queue.finish(true);
    expect(api.puts.map((p) => `${p.stream}/${p.seq}`).sort()).toEqual(["events/0", "events/1", "webcam/0"]);
    expect(api.completes).toEqual([{ last_seq: { events: 1, webcam: 0 }, interrupted: true }]);
    expect(await db.listSessions()).toEqual([]);
    expect(await db.chunksOf("sid")).toEqual([]);
  });

  it("calls /complete with the highest seq per stream and clears the session on success", async () => {
    const api = new StubApi();
    const queue = await open(api);
    await queue.enqueue("events", bytes(1));
    await queue.enqueue("events", bytes(2));
    await queue.finish(false);
    expect(api.completes).toEqual([{ last_seq: { events: 1 }, interrupted: false }]);
    expect(await db.listSessions()).toEqual([]);
  });

  it("re-sends the missing seqs the server reports and completes again", async () => {
    const api = new StubApi(undefined, (_req, call) =>
      call === 0 ? { status: "incomplete", missing: { events: [1] } } : { status: "uploaded", missing: {} },
    );
    const queue = await open(api);
    await queue.enqueue("events", bytes(1));
    const second = bytes(2);
    await queue.enqueue("events", second);
    await queue.idle();
    // Simulate a chunk that is still stored although the server lacks it (e.g. its delete failed).
    await db.putChunk({ sessionId: "sid", stream: "events", seq: 1, sha256: await sha256Hex(second), bytes: second }, {
      ...SESSION,
      nextSeq: { events: 2 },
    });
    api.puts.length = 0;

    await queue.finish(false);
    expect(api.puts.map((p) => `${p.stream}/${p.seq}`)).toEqual(["events/1"]);
    expect(api.completes).toHaveLength(2);
    expect(await db.listSessions()).toEqual([]);
  });

  it("fails loudly when the server misses a chunk that is no longer stored", async () => {
    const api = new StubApi(undefined, () => ({ status: "incomplete", missing: { events: [0] } }));
    const queue = await open(api);
    await queue.enqueue("events", bytes(1));
    await expect(queue.finish(false)).rejects.toThrow(/events chunk 0/);
  });

  it("fails loudly when the server says incomplete without naming a chunk", async () => {
    const api = new StubApi(undefined, () => ({ status: "incomplete", missing: {} }));
    const queue = await open(api);
    await expect(queue.finish(false)).rejects.toThrow(/names no missing chunk/);
  });

  it("retries /complete on a network error", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const api = new StubApi(undefined, (_r, call) => {
      if (call === 0) throw new ApiError(0, "offline");
      return { status: "uploaded", missing: {} };
    });
    const queue = await open(api);
    const finished = queue.finish(false);
    await vi.advanceTimersByTimeAsync(UPLOAD_BACKOFF_BASE_MS);
    await finished;
    expect(api.completes).toHaveLength(2);
  });
});

async function gunzipText(data: Uint8Array): Promise<string> {
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Response(stream).text();
}

describe("EventsUploader", () => {
  it("makes one gzipped JSON-lines chunk per interval, none for an empty interval, seqs contiguous", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    const api = new StubApi();
    const queue = await open(api);
    const chunkMs = 2000;
    const uploader = new EventsUploader(queue, chunkMs, (e) => {
      throw e;
    });
    const log = new EventLog(uploader.push);
    uploader.start();

    log.log("window_blur", {});
    log.log("window_focus", {});
    await vi.advanceTimersByTimeAsync(chunkMs);
    await uploader.flush(); // waits for the interval's flush to finish
    await queue.idle();
    expect(api.puts.map((p) => p.seq)).toEqual([0]);

    await vi.advanceTimersByTimeAsync(chunkMs); // empty interval
    await uploader.flush();
    await queue.idle();
    expect(api.puts).toHaveLength(1);

    log.log("fullscreen_change", { fullscreen: true });
    await vi.advanceTimersByTimeAsync(chunkMs);
    await uploader.flush();
    await queue.idle();
    expect(api.puts.map((p) => p.seq)).toEqual([0, 1]);

    const lines = (await gunzipText(api.puts[0]?.bytes ?? bytes())).split("\n");
    expect(lines.pop()).toBe(""); // newline-terminated
    const records = lines.map((l) => JSON.parse(l) as EventRecord);
    expect(records.map((r) => r.type)).toEqual(["window_blur", "window_focus"]);
    expect(Object.keys(records[0] ?? {}).sort()).toEqual(["data", "t", "type"]);
    // sha256 is over the compressed bytes
    expect(api.puts[0]?.sha256).toBe(await sha256Hex(api.puts[0]?.bytes ?? bytes()));
    await uploader.stop();
  });

  it("stop() flushes what is buffered; flushing nothing makes no chunk", async () => {
    const api = new StubApi();
    const queue = await open(api);
    const uploader = new EventsUploader(queue, 2000, (e) => {
      throw e;
    });
    await uploader.stop();
    expect(api.puts).toHaveLength(0);
    const log = new EventLog(uploader.push);
    log.log("window_blur", {});
    expect(uploader.hasUnsent()).toBe(true);
    await uploader.stop();
    expect(uploader.hasUnsent()).toBe(false);
    await queue.idle();
    expect(api.puts.map((p) => p.seq)).toEqual([0]);
  });
});

describe("a failed IndexedDB write", () => {
  it("is fatal and loud: nothing is sent, the error is reported, later chunks are refused", async () => {
    const api = new StubApi();
    const errors: Error[] = [];
    const queue = await open(api, (e) => errors.push(e));
    vi.spyOn(db, "putChunk").mockRejectedValue(new Error("QuotaExceededError"));

    await expect(queue.enqueue("events", bytes(1))).rejects.toThrow("QuotaExceededError");
    expect(errors.map((e) => e.message)).toEqual(["QuotaExceededError"]);
    expect(api.puts).toHaveLength(0);
    // The chunk was never stored, so the queue must not carry on as if it had been.
    await expect(queue.enqueue("events", bytes(2))).rejects.toThrow("QuotaExceededError");
    await expect(queue.idle()).rejects.toThrow("QuotaExceededError");
    expect(queue.stats().error?.message).toBe("QuotaExceededError");
  });
});

