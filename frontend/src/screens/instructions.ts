// Instructions (PLAN §5.2 step 5).

import { infoScreen } from "../ui/dom";

export function instructionsScreen(root: HTMLElement): Promise<void> {
  return infoScreen(
    root,
    "Before you start",
    [
      "Keep your head still and stay at the current distance from the screen.",
      "Do not cover your face.",
      "Please do not leave fullscreen during the study.",
      "First you will see a series of dots. Look at each dot and click it once it has shrunk.",
    ],
    "I understand",
  );
}
