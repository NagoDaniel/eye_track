// Main-thread client of the face model worker (PLAN §5.4). The model runs in modelWorker.ts, a
// classic worker bundled to /mediapipe/face-worker.js (see scripts/build-face-worker.mjs).

import type { Landmark } from "../records";
import type { FaceResult } from "./featureRecord";
import type { ModelRequest, ModelResponse, RawMatrix, RawModelResult } from "./modelProtocol";

export const FACE_WORKER_URL = "/mediapipe/face-worker.js";
export const MEDIAPIPE_WASM_BASE = "/mediapipe/wasm";
export const FACE_MODEL_URL = "/mediapipe/face_landmarker.task";
/** Two, so that the device check can tell "only one person in view" (PLAN §5.2 step 4). */
export const MAX_FACES = 2;
const MATRIX_ROWS = 4;
const MESH_STRIDE = 3;

/** MediaPipe's browser API delivers the transformation matrix column-major (checked against the
 * Python API on the same clip, DECISIONS "Milestone 2 T6"); feature records hold it row-major. */
export function matrixToRowMajor(raw: RawMatrix): number[] {
  if (raw.rows !== MATRIX_ROWS || raw.columns !== MATRIX_ROWS || raw.data.length !== 16) {
    throw new Error(`unexpected face matrix ${raw.rows}x${raw.columns}`);
  }
  const rowMajor: number[] = [];
  for (let row = 0; row < MATRIX_ROWS; row++) {
    for (let col = 0; col < MATRIX_ROWS; col++) {
      rowMajor.push(raw.data[col * MATRIX_ROWS + row] as number);
    }
  }
  return rowMajor;
}

export interface ModelOutput {
  result: FaceResult;
  inferenceMs: number;
}

export function outputFromRaw(raw: RawModelResult): ModelOutput {
  let landmarks: Landmark[] | null = null;
  if (raw.landmarks !== null) {
    landmarks = [];
    for (let i = 0; i + 2 < raw.landmarks.length; i += MESH_STRIDE) {
      landmarks.push([
        raw.landmarks[i] as number,
        raw.landmarks[i + 1] as number,
        raw.landmarks[i + 2] as number,
      ]);
    }
  }
  return {
    result: {
      faces: raw.faces,
      landmarks,
      matrix: raw.matrix === null ? null : matrixToRowMajor(raw.matrix),
      blinkL: raw.blinkL,
      blinkR: raw.blinkR,
    },
    inferenceMs: raw.inferenceMs,
  };
}

interface Pending {
  resolve: (output: ModelOutput) => void;
  reject: (error: Error) => void;
}

export interface WorkerLike {
  postMessage(message: ModelRequest, transfer?: Transferable[]): void;
  terminate(): void;
  onmessage: ((event: MessageEvent<ModelResponse>) => void) | null;
  onerror: ((event: ErrorEvent) => void) | null;
}

export class FaceModel {
  private nextId = 0;
  private lastTimestamp = -1;
  private readonly pending = new Map<number, Pending>();

  private constructor(private readonly worker: WorkerLike) {
    worker.onmessage = (event) => this.handle(event.data);
    worker.onerror = (event) =>
      this.failAll(new Error(`face model worker failed: ${event.message}`));
  }

  /** Start the worker and load the model; rejects with a clear error if either fails. */
  static async start(
    createWorker: () => WorkerLike = () => new Worker(FACE_WORKER_URL) as unknown as WorkerLike,
  ): Promise<FaceModel> {
    const worker = createWorker();
    await new Promise<void>((resolve, reject) => {
      worker.onmessage = (event) => {
        if (event.data.kind === "ready") resolve();
        else if (event.data.kind === "error") reject(new Error(event.data.message));
      };
      worker.onerror = (event) => reject(new Error(`face model worker failed: ${event.message}`));
      worker.postMessage({
        kind: "init",
        wasmBase: MEDIAPIPE_WASM_BASE,
        modelUrl: FACE_MODEL_URL,
        numFaces: MAX_FACES,
      });
    }).catch((e: unknown) => {
      worker.terminate();
      throw e;
    });
    return new FaceModel(worker);
  }

  /** Run the model on one frame; the bitmap is transferred to the worker (and closed there). */
  detect(bitmap: ImageBitmap, timestampMs: number): Promise<ModelOutput> {
    // MediaPipe wants strictly increasing integer timestamps in VIDEO mode.
    const timestamp = Math.max(this.lastTimestamp + 1, Math.round(timestampMs));
    this.lastTimestamp = timestamp;
    const id = this.nextId++;
    return new Promise<ModelOutput>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage({ kind: "detect", id, bitmap, timestampMs: timestamp }, [bitmap]);
    });
  }

  close(): void {
    this.failAll(new Error("the face model was closed"));
    this.worker.terminate();
  }

  private handle(message: ModelResponse): void {
    if (message.kind === "result") {
      const entry = this.pending.get(message.id);
      this.pending.delete(message.id);
      try {
        entry?.resolve(outputFromRaw(message.result));
      } catch (e) {
        entry?.reject(e instanceof Error ? e : new Error(String(e)));
      }
    } else if (message.kind === "error") {
      if (message.id === null) return this.failAll(new Error(message.message));
      const entry = this.pending.get(message.id);
      this.pending.delete(message.id);
      entry?.reject(new Error(message.message));
    }
  }

  private failAll(error: Error): void {
    for (const entry of this.pending.values()) entry.reject(error);
    this.pending.clear();
  }
}
