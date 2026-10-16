// Device check (PLAN §5.2 step 4): the camera preview, and a face that is found, one person, well
// lit, at a sensible distance and roughly frontal, for 3 seconds in a row. Messages say what to do.

import type { StudyConfig } from "../config";
import {
  DeviceCheckTracker,
  type QualityIssue,
  assessQuality,
  qualityMessage,
} from "../face/quality";
import type { TrackedFrame } from "../face/faceTracker";
import { CameraError } from "../media/camera";
import { button, card, el, p, render } from "../ui/dom";

/** What the screen needs from a running capture (`Capture` satisfies it). */
export interface CheckSource {
  stream: MediaStream;
  listeners: Set<(frame: TrackedFrame) => void>;
}

export const DEVICE_CHECK_INTRO =
  "Sit about an arm's length from the screen, face the camera, and make sure there is light in front of you.";
export const DEVICE_CHECK_OK = "Looking good. Hold still for a moment.";
export const DEVICE_CHECK_WAITING = "Looking for your face ...";

/** What is wrong with this frame (empty when nothing is). */
export function frameIssues(frame: TrackedFrame, limits: StudyConfig["quality"]): QualityIssue[] {
  return assessQuality(
    {
      faces: frame.result.faces,
      brightness: frame.brightness,
      faceRatio: frame.features?.faceRatio ?? null,
      yaw: frame.features?.yaw ?? null,
      pitch: frame.features?.pitch ?? null,
    },
    limits,
  );
}

/** The sentence under the preview: the first thing to fix, else encouragement. */
export function checkMessage(issues: readonly QualityIssue[] | null): string {
  if (issues === null) return DEVICE_CHECK_WAITING;
  const first = issues[0];
  return first === undefined ? DEVICE_CHECK_OK : qualityMessage(first);
}

/** Show why the camera could not be used and wait for "Try again". */
function cameraProblem(root: HTMLElement, error: CameraError): Promise<void> {
  return new Promise((resolve) => {
    render(
      root,
      card(
        "Camera check",
        p(error.message, "error"),
        button("Try again", () => resolve()),
      ),
    );
  });
}

/**
 * Open the camera (asking `open`), show the check and resolve with the capture once the check has
 * passed and the participant presses Continue. A camera problem is shown with "Try again".
 */
export async function deviceCheckScreen<C extends CheckSource>(
  root: HTMLElement,
  open: () => Promise<C>,
  limits: StudyConfig["quality"],
): Promise<{ capture: C; referenceFaceRatio: number | null }> {
  let capture: C | null = null;
  while (capture === null) {
    try {
      capture = await open();
    } catch (e) {
      if (!(e instanceof CameraError)) throw e;
      await cameraProblem(root, e);
    }
  }
  const source = capture;
  const preview = el("video", {
    class: "camera-preview",
    attrs: { autoplay: "", muted: "", playsinline: "" },
  });
  preview.muted = true;
  preview.srcObject = source.stream;
  const status = p(DEVICE_CHECK_WAITING);
  const bar = el("progress", { attrs: { max: "1", value: "0" } });
  const tracker = new DeviceCheckTracker();
  let ready = false;
  const next = button("Continue", () => finish());
  next.disabled = true;
  render(root, card("Camera check", p(DEVICE_CHECK_INTRO), preview, status, bar, next));

  let finish: () => void = () => undefined;
  const done = new Promise<void>((resolve) => {
    finish = resolve;
  });
  const listener = (frame: TrackedFrame): void => {
    const issues = frameIssues(frame, limits);
    status.textContent = checkMessage(issues);
    tracker.update(frame.t, issues, frame.features?.faceRatio ?? null);
    bar.value = tracker.progress;
    ready = ready || tracker.ready;
    next.disabled = !ready;
  };
  source.listeners.add(listener);
  await done;
  source.listeners.delete(listener);
  preview.srcObject = null;
  return { capture: source, referenceFaceRatio: tracker.medianFaceRatio };
}
