// Participant debug mode switches (PLAN §12.1): the URL flag, the keyboard shortcuts and the
// small state they drive. Pure; the controller (not this file) decides whether debug is allowed.

export const DEBUG_QUERY_PARAM = "debug";
export const DEBUG_QUERY_VALUE = "1";

/** Elements in which typing (or dragging a slider) must not trigger a shortcut. */
export const TYPING_TAGS: readonly string[] = ["INPUT", "TEXTAREA", "SELECT"];

export const DEBUG_ACTIONS = [
  "toggle_overlay",
  "toggle_gaze_dot",
  "skip",
  "force_gate_failure",
] as const;
export type DebugAction = (typeof DEBUG_ACTIONS)[number];

/** Lower-case key per action. */
const KEY_TO_ACTION: Readonly<Record<string, DebugAction>> = {
  d: "toggle_overlay",
  g: "toggle_gaze_dot",
  s: "skip",
  f: "force_gate_failure",
};

/** `?debug=1` in `location.search`. */
export function debugRequested(search: string): boolean {
  return new URLSearchParams(search).get(DEBUG_QUERY_PARAM) === DEBUG_QUERY_VALUE;
}

/** The fields of a KeyboardEvent that matter here. */
export interface KeyEventLike {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
}

/**
 * The action for a key press, or null. Ignored with ctrl/meta/alt held (browser shortcuts) and
 * while typing in an input, textarea or select (`targetTag` is the event target's tag name).
 */
export function debugKeyAction(
  event: KeyEventLike,
  targetTag: string | null = null,
): DebugAction | null {
  if (event.ctrlKey || event.metaKey || event.altKey) return null;
  if (targetTag !== null && TYPING_TAGS.includes(targetTag.toUpperCase())) return null;
  return KEY_TO_ACTION[event.key.toLowerCase()] ?? null;
}

export interface DebugState {
  overlayVisible: boolean;
  gazeDotVisible: boolean;
  /** `F`: calibration gate decisions fail on purpose until toggled off. */
  forceGateFailure: boolean;
  /** Counts `S` presses; the controller acts on each increment and logs `debug_skip`. */
  skipRequested: number;
}

export function initialDebugState(): DebugState {
  return { overlayVisible: true, gazeDotVisible: true, forceGateFailure: false, skipRequested: 0 };
}

export function reduceDebug(state: DebugState, action: DebugAction): DebugState {
  switch (action) {
    case "toggle_overlay":
      return { ...state, overlayVisible: !state.overlayVisible };
    case "toggle_gaze_dot":
      return { ...state, gazeDotVisible: !state.gazeDotVisible };
    case "force_gate_failure":
      return { ...state, forceGateFailure: !state.forceGateFailure };
    case "skip":
      return { ...state, skipRequested: state.skipRequested + 1 };
  }
}
