import { describe, expect, it } from "vitest";

import faceFrames from "../../../shared/fixtures/face_frames.json";
import { FaceModel, type WorkerLike, matrixToRowMajor, outputFromRaw } from "./faceModel";
import type { ModelRequest, ModelResponse, RawModelResult } from "./modelProtocol";

// The browser delivers the matrix column-major: translation at 12, 13, 14. The fixture (real
// MediaPipe output, row-major) is turned into that layout and must come back unchanged.
function toColumnMajor(rowMajor: number[]): number[] {
  const out: number[] = [];
  for (let col = 0; col < 4; col++)
    for (let row = 0; row < 4; row++) out.push(rowMajor[row * 4 + col] as number);
  return out;
}

describe("matrixToRowMajor", () => {
  it("turns the browser's column-major matrix into the row-major record layout (real matrices)", () => {
    for (const frame of faceFrames.frames) {
      const raw = { rows: 4, columns: 4, data: toColumnMajor(frame.matrix) };
      expect(matrixToRowMajor(raw)).toEqual(frame.matrix);
    }
  });

  it("puts the translation at indices 3, 7 and 11", () => {
    const columnMajor = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 5, 6, 7, 1];
    const rowMajor = matrixToRowMajor({ rows: 4, columns: 4, data: columnMajor });
    expect([rowMajor[3], rowMajor[7], rowMajor[11]]).toEqual([5, 6, 7]);
  });

  it("refuses anything but 4x4", () => {
    expect(() =>
      matrixToRowMajor({ rows: 3, columns: 4, data: new Array<number>(12).fill(0) }),
    ).toThrow(/3x4/);
    expect(() => matrixToRowMajor({ rows: 4, columns: 4, data: [1, 2] })).toThrow();
  });
});

describe("outputFromRaw", () => {
  const raw: RawModelResult = {
    faces: 1,
    landmarks: [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
    matrix: {
      rows: 4,
      columns: 4,
      data: toColumnMajor([1, 0, 0, 9, 0, 1, 0, 8, 0, 0, 1, 7, 0, 0, 0, 1]),
    },
    blinkL: 0.1,
    blinkR: 0.2,
    inferenceMs: 12.5,
  };

  it("groups the flat landmarks in threes and converts the matrix", () => {
    const { result, inferenceMs } = outputFromRaw(raw);
    expect(result.landmarks).toEqual([
      [0.1, 0.2, 0.3],
      [0.4, 0.5, 0.6],
    ]);
    expect(result.matrix?.[3]).toBe(9);
    expect(result).toMatchObject({ faces: 1, blinkL: 0.1, blinkR: 0.2 });
    expect(inferenceMs).toBe(12.5);
  });

  it("a frame without a face has no landmarks and no matrix", () => {
    const { result } = outputFromRaw({ ...raw, faces: 0, landmarks: null, matrix: null });
    expect(result).toMatchObject({ faces: 0, landmarks: null, matrix: null });
  });
});

class FakeWorker implements WorkerLike {
  onmessage: ((event: MessageEvent<ModelResponse>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  sent: ModelRequest[] = [];
  terminated = false;
  constructor(private readonly onRequest: (request: ModelRequest, worker: FakeWorker) => void) {}
  postMessage(message: ModelRequest): void {
    this.sent.push(message);
    queueMicrotask(() => this.onRequest(message, this));
  }
  terminate(): void {
    this.terminated = true;
  }
  reply(message: ModelResponse): void {
    this.onmessage?.({ data: message } as MessageEvent<ModelResponse>);
  }
}

const bitmap = { close: () => undefined } as unknown as ImageBitmap;
const RAW_NO_FACE: RawModelResult = {
  faces: 0,
  landmarks: null,
  matrix: null,
  blinkL: 0,
  blinkR: 0,
  inferenceMs: 3,
};

describe("FaceModel", () => {
  it("starts the worker with the self-hosted files and answers detect calls by id", async () => {
    const worker = new FakeWorker((request, w) => {
      if (request.kind === "init") w.reply({ kind: "ready" });
      else w.reply({ kind: "result", id: request.id, result: RAW_NO_FACE });
    });
    const model = await FaceModel.start(() => worker);
    expect(worker.sent[0]).toEqual({
      kind: "init",
      wasmBase: "/mediapipe/wasm",
      modelUrl: "/mediapipe/face_landmarker.task",
      numFaces: 2,
    });
    const output = await model.detect(bitmap, 100.4);
    expect(output.inferenceMs).toBe(3);
    expect(output.result.faces).toBe(0);
  });

  it("sends strictly increasing integer timestamps", async () => {
    const worker = new FakeWorker((request, w) => {
      if (request.kind === "init") w.reply({ kind: "ready" });
      else w.reply({ kind: "result", id: request.id, result: RAW_NO_FACE });
    });
    const model = await FaceModel.start(() => worker);
    await model.detect(bitmap, 100.2);
    await model.detect(bitmap, 100.4); // rounds to the same value
    await model.detect(bitmap, 50); // the clock never goes back, but be safe
    const stamps = worker.sent.flatMap((m) => (m.kind === "detect" ? [m.timestampMs] : []));
    expect(stamps).toEqual([100, 101, 102]);
  });

  it("a failing init rejects with the worker's message and ends the worker", async () => {
    const worker = new FakeWorker((request, w) => {
      if (request.kind === "init")
        w.reply({ kind: "error", id: null, message: "wasm failed to load" });
    });
    await expect(FaceModel.start(() => worker)).rejects.toThrow("wasm failed to load");
    expect(worker.terminated).toBe(true);
  });

  it("a detect error rejects only that call; a worker crash rejects every pending call", async () => {
    let crash = false;
    const worker = new FakeWorker((request, w) => {
      if (request.kind === "init") w.reply({ kind: "ready" });
      else if (request.kind === "detect" && request.id === 0)
        w.reply({ kind: "error", id: 0, message: "bad frame" });
      else if (crash) w.onerror?.({ message: "boom" } as ErrorEvent);
    });
    const model = await FaceModel.start(() => worker);
    await expect(model.detect(bitmap, 1)).rejects.toThrow("bad frame");
    crash = true;
    await expect(model.detect(bitmap, 2)).rejects.toThrow("boom");
  });

  it("close rejects pending calls and terminates the worker", async () => {
    const worker = new FakeWorker((request, w) => {
      if (request.kind === "init") w.reply({ kind: "ready" });
    });
    const model = await FaceModel.start(() => worker);
    const pending = model.detect(bitmap, 1);
    model.close();
    await expect(pending).rejects.toThrow("closed");
    expect(worker.terminated).toBe(true);
  });
});
