// Messages between faceModel.ts (main thread) and modelWorker.ts (PLAN §5.4).

export type ModelRequest =
  | { kind: "init"; wasmBase: string; modelUrl: string; numFaces: number }
  | { kind: "detect"; id: number; bitmap: ImageBitmap; timestampMs: number };

/** The matrix exactly as MediaPipe delivers it; faceModel.ts converts it to row-major. */
export interface RawMatrix {
  rows: number;
  columns: number;
  data: number[];
}

export interface RawModelResult {
  faces: number;
  /** x, y, z of every mesh point of the first face, flat; null without a face. */
  landmarks: number[] | null;
  matrix: RawMatrix | null;
  blinkL: number;
  blinkR: number;
  inferenceMs: number;
}

export type ModelResponse =
  | { kind: "ready" }
  | { kind: "result"; id: number; result: RawModelResult }
  | { kind: "error"; id: number | null; message: string };
