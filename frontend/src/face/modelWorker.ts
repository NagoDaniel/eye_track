// Web Worker that runs the MediaPipe Face Landmarker off the main thread (PLAN §5.4), so a slow
// inference never makes the stimulus video drop frames. Talks to faceModel.ts through the message
// types below. CPU delegate only (headless CI has no GPU).

import { FaceLandmarker, FilesetResolver } from "@mediapipe/tasks-vision";

import type { ModelRequest, ModelResponse, RawModelResult } from "./modelProtocol";

let landmarker: FaceLandmarker | null = null;

function reply(message: ModelResponse, transfer: Transferable[] = []): void {
  (self as unknown as Worker).postMessage(message, transfer);
}

async function init(wasmBase: string, modelUrl: string, numFaces: number): Promise<void> {
  const vision = await FilesetResolver.forVisionTasks(wasmBase);
  landmarker = await FaceLandmarker.createFromOptions(vision, {
    baseOptions: { modelAssetPath: modelUrl, delegate: "CPU" },
    runningMode: "VIDEO",
    numFaces,
    outputFaceBlendshapes: true,
    outputFacialTransformationMatrixes: true,
  });
}

function detect(bitmap: ImageBitmap, timestampMs: number): RawModelResult {
  if (landmarker === null) throw new Error("the face model is not initialised");
  const started = performance.now();
  const result = landmarker.detectForVideo(bitmap, timestampMs);
  const inferenceMs = performance.now() - started;
  bitmap.close();
  const first = result.faceLandmarks[0];
  const matrix = result.facialTransformationMatrixes[0];
  const blendshapes = result.faceBlendshapes[0]?.categories ?? [];
  // Without a face there are no blendshapes (and nothing reads the scores). With a face a missing
  // score would read as "no blink", so it is an error.
  const score = (name: string): number => {
    if (first === undefined) return 0;
    const found = blendshapes.find((c) => c.categoryName === name);
    if (found === undefined) throw new Error(`the face model gave no ${name} blendshape`);
    return found.score;
  };
  return {
    faces: result.faceLandmarks.length,
    landmarks: first === undefined ? null : first.flatMap((p) => [p.x, p.y, p.z]),
    matrix:
      matrix === undefined
        ? null
        : { rows: matrix.rows, columns: matrix.columns, data: Array.from(matrix.data) },
    blinkL: score("eyeBlinkLeft"),
    blinkR: score("eyeBlinkRight"),
    inferenceMs,
  };
}

self.onmessage = (event: MessageEvent<ModelRequest>) => {
  const request = event.data;
  if (request.kind === "init") {
    init(request.wasmBase, request.modelUrl, request.numFaces).then(
      () => reply({ kind: "ready" }),
      (e: unknown) =>
        reply({ kind: "error", id: null, message: e instanceof Error ? e.message : String(e) }),
    );
    return;
  }
  try {
    reply({ kind: "result", id: request.id, result: detect(request.bitmap, request.timestampMs) });
  } catch (e) {
    reply({ kind: "error", id: request.id, message: e instanceof Error ? e.message : String(e) });
  }
};
