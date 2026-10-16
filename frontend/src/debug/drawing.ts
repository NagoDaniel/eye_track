// Pure canvas drawing for the debug overlay (PLAN §12.1). Points are normalised (0..1) and scaled
// by `size` (the canvas size in pixels). The context is a minimal interface so tests can record
// calls; a CanvasRenderingContext2D satisfies it.

export interface CanvasLike {
  strokeStyle: string | CanvasGradient | CanvasPattern;
  fillStyle: string | CanvasGradient | CanvasPattern;
  lineWidth: number;
  clearRect(x: number, y: number, w: number, h: number): void;
  fillRect(x: number, y: number, w: number, h: number): void;
  strokeRect(x: number, y: number, w: number, h: number): void;
  beginPath(): void;
  moveTo(x: number, y: number): void;
  lineTo(x: number, y: number): void;
  arc(x: number, y: number, radius: number, start: number, end: number): void;
  stroke(): void;
  fill(): void;
}

export interface NormPoint {
  x: number;
  y: number;
}
export interface NormBox {
  x: number;
  y: number;
  width: number;
  height: number;
}
export interface PixelSize {
  width: number;
  height: number;
}

export const LANDMARK_COLOR = "#4fd1c5";
export const IRIS_COLOR = "#ffd24a";
export const FACE_BOX_COLOR = "#7aa2ff";
export const LANDMARK_SIZE_PX = 2;
export const IRIS_RADIUS_PX = 4;
export const GATE_TARGET_COLOR = "#5dff7a";
export const GATE_PREDICTION_COLOR = "#ff5d5d";
export const GATE_MARK_RADIUS_PX = 6;
export const LINE_WIDTH_PX = 1.5;

export interface FaceDrawing {
  landmarks: readonly NormPoint[];
  irisCenters: readonly NormPoint[];
  box: NormBox | null;
}

/** Clears the canvas and draws the face box, every landmark as a small square and the iris centres. */
export function drawFaceOverlay(ctx: CanvasLike, face: FaceDrawing | null, size: PixelSize): void {
  ctx.clearRect(0, 0, size.width, size.height);
  if (face === null) return;
  ctx.lineWidth = LINE_WIDTH_PX;
  if (face.box !== null) {
    ctx.strokeStyle = FACE_BOX_COLOR;
    ctx.strokeRect(
      face.box.x * size.width,
      face.box.y * size.height,
      face.box.width * size.width,
      face.box.height * size.height,
    );
  }
  ctx.fillStyle = LANDMARK_COLOR;
  const half = LANDMARK_SIZE_PX / 2;
  for (const p of face.landmarks) {
    ctx.fillRect(
      p.x * size.width - half,
      p.y * size.height - half,
      LANDMARK_SIZE_PX,
      LANDMARK_SIZE_PX,
    );
  }
  ctx.strokeStyle = IRIS_COLOR;
  for (const p of face.irisCenters) {
    ctx.beginPath();
    ctx.arc(p.x * size.width, p.y * size.height, IRIS_RADIUS_PX, 0, 2 * Math.PI);
    ctx.stroke();
  }
}

/** One validation point: where it was shown and the median prediction (null: no usable frames). */
export interface GateErrorPoint {
  target: NormPoint;
  prediction: NormPoint | null;
}

/** Clears the canvas and draws, per point, a line from the target to the median prediction. */
export function drawGateErrors(
  ctx: CanvasLike,
  points: readonly GateErrorPoint[],
  size: PixelSize,
): void {
  ctx.clearRect(0, 0, size.width, size.height);
  ctx.lineWidth = LINE_WIDTH_PX;
  for (const { target, prediction } of points) {
    const tx = target.x * size.width;
    const ty = target.y * size.height;
    if (prediction !== null) {
      const px = prediction.x * size.width;
      const py = prediction.y * size.height;
      ctx.strokeStyle = GATE_PREDICTION_COLOR;
      ctx.beginPath();
      ctx.moveTo(tx, ty);
      ctx.lineTo(px, py);
      ctx.stroke();
      ctx.fillStyle = GATE_PREDICTION_COLOR;
      ctx.fillRect(px - 2, py - 2, 4, 4);
    }
    ctx.strokeStyle = GATE_TARGET_COLOR;
    ctx.beginPath();
    ctx.arc(tx, ty, GATE_MARK_RADIUS_PX, 0, 2 * Math.PI);
    ctx.stroke();
  }
}
