// Hand-written IndexedDB wrapper for the upload queue (PLAN §5.8, §3: no `idb` dependency).
// Two stores: `chunks` (bytes waiting for a 2xx) and `sessions` (what is needed to resume an upload).

import { UPLOAD_DB_NAME, UPLOAD_DB_VERSION } from "../constants";
import type { Stream } from "../eventLog";

export interface StoredChunk {
  sessionId: string;
  stream: Stream;
  seq: number;
  /** Lowercase hex sha256 of `bytes`, sent as `X-Chunk-SHA256`. */
  sha256: string;
  bytes: Uint8Array;
}

/** Enough state to end an unfinished session after a page reload. */
export interface StoredSession {
  sessionId: string;
  uploadToken: string;
  studyCode: string;
  /** The seq the next chunk of each stream gets; chunks are contiguous from 0. */
  nextSeq: Partial<Record<Stream, number>>;
}

const CHUNKS = "chunks";
const SESSIONS = "sessions";
const BY_SESSION = "bySession";

function done(request: IDBRequest): Promise<void> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function result<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function committed(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error ?? new Error("IndexedDB transaction failed"));
    tx.onabort = () => reject(tx.error ?? new Error("IndexedDB transaction aborted"));
  });
}

export class UploadDb {
  private constructor(private readonly db: IDBDatabase) {}

  static open(factory: IDBFactory = indexedDB, name: string = UPLOAD_DB_NAME): Promise<UploadDb> {
    return new Promise((resolve, reject) => {
      const request = factory.open(name, UPLOAD_DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        const chunks = db.createObjectStore(CHUNKS, { keyPath: ["sessionId", "stream", "seq"] });
        chunks.createIndex(BY_SESSION, "sessionId");
        db.createObjectStore(SESSIONS, { keyPath: "sessionId" });
      };
      request.onsuccess = () => resolve(new UploadDb(request.result));
      request.onerror = () => reject(request.error ?? new Error("Cannot open IndexedDB"));
    });
  }

  close(): void {
    this.db.close();
  }

  /** Store a chunk and the session's updated `nextSeq` in one transaction. */
  async putChunk(chunk: StoredChunk, session: StoredSession): Promise<void> {
    const tx = this.db.transaction([CHUNKS, SESSIONS], "readwrite");
    tx.objectStore(CHUNKS).put(chunk);
    tx.objectStore(SESSIONS).put(session);
    await committed(tx);
  }

  async putSession(session: StoredSession): Promise<void> {
    await done(this.db.transaction(SESSIONS, "readwrite").objectStore(SESSIONS).put(session));
  }

  async listSessions(): Promise<StoredSession[]> {
    return result(this.db.transaction(SESSIONS).objectStore(SESSIONS).getAll() as IDBRequest<StoredSession[]>);
  }

  /** The session's stored chunks, ordered by stream then seq. */
  async chunksOf(sessionId: string): Promise<StoredChunk[]> {
    const index = this.db.transaction(CHUNKS).objectStore(CHUNKS).index(BY_SESSION);
    const chunks = await result(index.getAll(sessionId) as IDBRequest<StoredChunk[]>);
    return chunks.sort((a, b) => (a.stream === b.stream ? a.seq - b.seq : a.stream < b.stream ? -1 : 1));
  }

  async getChunk(sessionId: string, stream: Stream, seq: number): Promise<StoredChunk | undefined> {
    const store = this.db.transaction(CHUNKS).objectStore(CHUNKS);
    return (await result(store.get([sessionId, stream, seq]))) as StoredChunk | undefined;
  }

  async deleteChunk(chunk: StoredChunk): Promise<void> {
    const store = this.db.transaction(CHUNKS, "readwrite").objectStore(CHUNKS);
    await done(store.delete([chunk.sessionId, chunk.stream, chunk.seq]));
  }

  /** Remove the session record and any chunks left for it. */
  async deleteSession(sessionId: string): Promise<void> {
    const chunks = await this.chunksOf(sessionId);
    const tx = this.db.transaction([CHUNKS, SESSIONS], "readwrite");
    for (const c of chunks) tx.objectStore(CHUNKS).delete([c.sessionId, c.stream, c.seq]);
    tx.objectStore(SESSIONS).delete(sessionId);
    await committed(tx);
  }
}
