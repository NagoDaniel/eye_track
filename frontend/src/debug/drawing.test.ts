import { describe, expect, it } from "vitest";

import {
  type CanvasLike,
  GATE_PREDICTION_COLOR,
  GATE_TARGET_COLOR,
  IRIS_COLOR,
  drawFaceOverlay,
  drawGateErrors,
} from "./drawing";

type Call = [string, ...number[]];

/** Records every method call and the style values in force at each drawing call. */
class RecordingContext implements CanvasLike {
  calls: Call[] = [];
  styles: { call: string; stroke: unknown; fill: unknown }[] = [];
  strokeStyle: string | CanvasGradient | CanvasPattern = "";
  fillStyle: string | CanvasGradient | CanvasPattern = "";
  lineWidth = 1;
  private rec(name: string, ...args: number[]): void {
    this.calls.push([name, ...args]);
    this.styles.push({ call: name, stroke: this.strokeStyle, fill: this.fillStyle });
  }
  clearRect(x: number, y: number, w: number, h: number): void {
    this.rec("clearRect", x, y, w, h);
  }
  fillRect(x: number, y: number, w: number, h: number): void {
    this.rec("fillRect", x, y, w, h);
  }
  strokeRect(x: number, y: number, w: number, h: number): void {
    this.rec("strokeRect", x, y, w, h);
  }
  beginPath(): void {
    this.rec("beginPath");
  }
  moveTo(x: number, y: number): void {
    this.rec("moveTo", x, y);
  }
  lineTo(x: number, y: number): void {
    this.rec("lineTo", x, y);
  }
  arc(x: number, y: number, r: number, s: number, e: number): void {
    this.rec("arc", x, y, r, s, e);
  }
  stroke(): void {
    this.rec("stroke");
  }
  fill(): void {
    this.rec("fill");
  }
  names(): string[] {
    return this.calls.map((c) => c[0]);
  }
}

const size = { width: 200, height: 100 };

describe("drawFaceOverlay", () => {
  it("only clears the canvas when there is no face", () => {
    const ctx = new RecordingContext();
    drawFaceOverlay(ctx, null, size);
    expect(ctx.calls).toEqual([["clearRect", 0, 0, 200, 100]]);
  });

  it("draws the box, one square per landmark and a circle per iris, scaled to the canvas", () => {
    const ctx = new RecordingContext();
    drawFaceOverlay(
      ctx,
      {
        box: { x: 0.25, y: 0.1, width: 0.5, height: 0.8 },
        landmarks: [
          { x: 0.5, y: 0.5 },
          { x: 0, y: 1 },
        ],
        irisCenters: [{ x: 0.4, y: 0.5 }],
      },
      size,
    );
    expect(ctx.calls[0]).toEqual(["clearRect", 0, 0, 200, 100]);
    expect(ctx.calls).toContainEqual(["strokeRect", 50, 10, 100, 80]);
    const squares = ctx.calls.filter((c) => c[0] === "fillRect");
    expect(squares).toHaveLength(2);
    expect(squares[0]).toEqual(["fillRect", 99, 49, 2, 2]);
    expect(squares[1]).toEqual(["fillRect", -1, 99, 2, 2]);
    const arcs = ctx.calls.filter((c) => c[0] === "arc");
    expect(arcs).toHaveLength(1);
    expect(arcs[0]?.slice(1, 3)).toEqual([80, 50]);
    expect(ctx.styles.find((s) => s.call === "arc")?.stroke).toBe(IRIS_COLOR);
  });

  it("skips the box when it is null", () => {
    const ctx = new RecordingContext();
    drawFaceOverlay(ctx, { box: null, landmarks: [], irisCenters: [] }, size);
    expect(ctx.names()).toEqual(["clearRect"]);
  });
});

describe("drawGateErrors", () => {
  it("draws a line from each target to its median prediction and marks the target", () => {
    const ctx = new RecordingContext();
    drawGateErrors(
      ctx,
      [
        { target: { x: 0.5, y: 0.5 }, prediction: { x: 0.6, y: 0.4 } },
        { target: { x: 0.1, y: 0.1 }, prediction: null },
      ],
      size,
    );
    expect(ctx.calls[0]).toEqual(["clearRect", 0, 0, 200, 100]);
    expect(ctx.calls).toContainEqual(["moveTo", 100, 50]);
    expect(ctx.calls).toContainEqual(["lineTo", 120, 40]);
    // One line only: the second point has no prediction. Both targets are marked.
    expect(ctx.calls.filter((c) => c[0] === "lineTo")).toHaveLength(1);
    const arcs = ctx.calls.filter((c) => c[0] === "arc");
    expect(arcs.map((a) => a.slice(1, 3))).toEqual([
      [100, 50],
      [20, 10],
    ]);
    expect(ctx.styles.find((s) => s.call === "lineTo")?.stroke).toBe(GATE_PREDICTION_COLOR);
    expect(ctx.styles.find((s) => s.call === "arc")?.stroke).toBe(GATE_TARGET_COLOR);
  });

  it("clears the canvas when there are no points", () => {
    const ctx = new RecordingContext();
    drawGateErrors(ctx, [], size);
    expect(ctx.names()).toEqual(["clearRect"]);
  });
});
