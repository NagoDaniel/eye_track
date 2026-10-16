// The live in-browser gaze prediction as a dot (PLAN §12.1, toggle `G`). Debug mode only.

export const GAZE_DOT_DIAMETER_PX = 16;
export const GAZE_DOT_COLOR = "rgba(255, 60, 60, 0.75)";
const GAZE_DOT_Z_INDEX = "2147483000";

export interface GazeDot {
  /** Move the dot to viewport pixel coordinates and show it. */
  show(x: number, y: number): void;
  hide(): void;
  destroy(): void;
}

export function createGazeDot(root: HTMLElement): GazeDot {
  const dot = document.createElement("div");
  dot.className = "debug-gaze-dot";
  const r = GAZE_DOT_DIAMETER_PX / 2;
  Object.assign(dot.style, {
    position: "fixed",
    left: "0",
    top: "0",
    width: `${GAZE_DOT_DIAMETER_PX}px`,
    height: `${GAZE_DOT_DIAMETER_PX}px`,
    marginLeft: `${-r}px`,
    marginTop: `${-r}px`,
    borderRadius: "50%",
    background: GAZE_DOT_COLOR,
    pointerEvents: "none",
    zIndex: GAZE_DOT_Z_INDEX,
    display: "none",
  });
  root.append(dot);
  return {
    show(x, y) {
      dot.style.transform = `translate(${x}px, ${y}px)`;
      dot.style.display = "block";
    },
    hide() {
      dot.style.display = "none";
    },
    destroy() {
      dot.remove();
    },
  };
}
