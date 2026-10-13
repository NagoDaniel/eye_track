// @vitest-environment jsdom
import { IDBFactory } from "fake-indexeddb";
import { describe, expect, it } from "vitest";

import type { CompleteRequest } from "../config";
import { EventLog } from "../eventLog";
import { EventsUploader } from "../upload/eventsUploader";
import { UploadDb } from "../upload/idb";
import { UploadQueue } from "../upload/uploadQueue";
import { uploadProgress, uploadScreen } from "./upload";

describe("uploadProgress", () => {
  it("is the share of chunks already sent", () => {
    expect(uploadProgress(4, 4)).toBe(0);
    expect(uploadProgress(4, 1)).toBe(0.75);
    expect(uploadProgress(4, 0)).toBe(1);
    expect(uploadProgress(0, 0)).toBe(1);
  });
});

describe("uploadScreen", () => {
  it("flushes the events, uploads them, completes the session and shows progress", async () => {
    document.body.innerHTML = '<main id="app"></main>';
    const root = document.getElementById("app") as HTMLElement;
    const db = await UploadDb.open(new IDBFactory());
    const puts: number[] = [];
    const completes: CompleteRequest[] = [];
    const api = {
      putChunk: (_s: string, _t: string, stream: "events", seq: number, bytes: Uint8Array) => {
        puts.push(seq);
        return Promise.resolve({ stream, seq, size: bytes.length });
      },
      complete: (_s: string, _t: string, request: CompleteRequest) => {
        completes.push(request);
        return Promise.resolve({ status: "uploaded" as const, missing: {} });
      },
    };
    const queue = await UploadQueue.open(db, api, { sessionId: "s", uploadToken: "t", studyCode: "c", nextSeq: {} });
    const events = new EventsUploader(queue, 2000, (e) => {
      throw e;
    });
    new EventLog(events.push).log("window_blur", {});

    await uploadScreen(root, queue, { events, interrupted: false });

    expect(puts).toEqual([0]);
    expect(completes).toEqual([{ last_seq: { events: 0 }, interrupted: false }]);
    expect(root.querySelector("progress")?.value).toBe(1);
  });
});
