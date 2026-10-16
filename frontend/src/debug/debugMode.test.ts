import { describe, expect, it } from "vitest";

import {
  DEBUG_ACTIONS,
  type KeyEventLike,
  debugKeyAction,
  debugRequested,
  initialDebugState,
  reduceDebug,
} from "./debugMode";

const key = (k: string, mods: Partial<KeyEventLike> = {}): KeyEventLike => ({
  key: k,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  ...mods,
});

describe("debugRequested", () => {
  it("is true only for debug=1", () => {
    expect(debugRequested("?debug=1")).toBe(true);
    expect(debugRequested("?p=AB0315&debug=1")).toBe(true);
    expect(debugRequested("")).toBe(false);
    expect(debugRequested("?debug=0")).toBe(false);
    expect(debugRequested("?debug=true")).toBe(false);
    expect(debugRequested("?nodebug=1")).toBe(false);
  });
});

describe("debugKeyAction", () => {
  it("maps D, G, S and F, in either case", () => {
    expect(debugKeyAction(key("d"))).toBe("toggle_overlay");
    expect(debugKeyAction(key("D"))).toBe("toggle_overlay");
    expect(debugKeyAction(key("g"))).toBe("toggle_gaze_dot");
    expect(debugKeyAction(key("G"))).toBe("toggle_gaze_dot");
    expect(debugKeyAction(key("s"))).toBe("skip");
    expect(debugKeyAction(key("S"))).toBe("skip");
    expect(debugKeyAction(key("f"))).toBe("force_gate_failure");
    expect(debugKeyAction(key("F"))).toBe("force_gate_failure");
  });

  it("ignores other keys", () => {
    for (const k of ["a", "Escape", "Enter", " ", "1", "Shift"]) {
      expect(debugKeyAction(key(k))).toBeNull();
    }
  });

  it("ignores ctrl, meta and alt combinations", () => {
    for (const mod of ["ctrlKey", "metaKey", "altKey"] as const) {
      expect(debugKeyAction(key("s", { [mod]: true }))).toBeNull();
      expect(debugKeyAction(key("d", { [mod]: true }))).toBeNull();
    }
  });

  it("ignores typing in inputs, textareas and selects (sliders are inputs)", () => {
    expect(debugKeyAction(key("d"), "INPUT")).toBeNull();
    expect(debugKeyAction(key("s"), "textarea")).toBeNull();
    expect(debugKeyAction(key("f"), "SELECT")).toBeNull();
    expect(debugKeyAction(key("d"), "BUTTON")).toBe("toggle_overlay");
    expect(debugKeyAction(key("d"), "BODY")).toBe("toggle_overlay");
    expect(debugKeyAction(key("d"), null)).toBe("toggle_overlay");
  });
});

describe("reduceDebug", () => {
  it("starts with overlay and gaze dot visible, no forced failure, no skips", () => {
    expect(initialDebugState()).toEqual({
      overlayVisible: true,
      gazeDotVisible: true,
      forceGateFailure: false,
      skipRequested: 0,
    });
  });

  it("toggles each switch and counts skips, without mutating the old state", () => {
    const s0 = initialDebugState();
    const s1 = reduceDebug(s0, "toggle_overlay");
    expect(s1.overlayVisible).toBe(false);
    expect(s0.overlayVisible).toBe(true);
    expect(reduceDebug(s1, "toggle_overlay").overlayVisible).toBe(true);
    expect(reduceDebug(s0, "toggle_gaze_dot").gazeDotVisible).toBe(false);
    expect(reduceDebug(s0, "force_gate_failure").forceGateFailure).toBe(true);
    const skipped = reduceDebug(reduceDebug(s0, "skip"), "skip");
    expect(skipped.skipRequested).toBe(2);
    expect(skipped.overlayVisible).toBe(true);
  });

  it("handles every action", () => {
    for (const action of DEBUG_ACTIONS) {
      expect(reduceDebug(initialDebugState(), action)).not.toEqual(initialDebugState());
    }
  });
});
