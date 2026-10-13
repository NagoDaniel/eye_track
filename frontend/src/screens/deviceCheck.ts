// Device check (PLAN §5.2 step 4). Placeholder in Milestone 1b: the camera and face checks arrive in M2.

import { infoScreen } from "../ui/dom";

export function deviceCheckScreen(root: HTMLElement): Promise<void> {
  return infoScreen(
    root,
    "Device check",
    ["The camera and lighting check will appear here in a later version of this study."],
    "Continue",
  );
}
