// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";

import { createGazeDot } from "./gazeDot";

let root: HTMLElement;
beforeEach(() => {
  document.body.innerHTML = '<main id="app"></main>';
  root = document.getElementById("app") as HTMLElement;
});

describe("createGazeDot", () => {
  it("starts hidden, ignores the pointer and is fixed-position", () => {
    createGazeDot(root);
    const dot = root.querySelector<HTMLElement>(".debug-gaze-dot");
    expect(dot).not.toBeNull();
    expect(dot?.style.display).toBe("none");
    expect(dot?.style.pointerEvents).toBe("none");
    expect(dot?.style.position).toBe("fixed");
  });

  it("show moves and shows it, hide hides it", () => {
    const gaze = createGazeDot(root);
    const dot = root.querySelector<HTMLElement>(".debug-gaze-dot");
    gaze.show(120, 80);
    expect(dot?.style.display).toBe("block");
    expect(dot?.style.transform).toBe("translate(120px, 80px)");
    gaze.show(5, 6);
    expect(dot?.style.transform).toBe("translate(5px, 6px)");
    gaze.hide();
    expect(dot?.style.display).toBe("none");
  });

  it("destroy removes the element", () => {
    const gaze = createGazeDot(root);
    gaze.destroy();
    expect(root.children).toHaveLength(0);
  });
});
