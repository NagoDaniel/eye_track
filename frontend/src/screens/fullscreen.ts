// Fullscreen screen (PLAN §5.2 step 6). Skipped when the study does not require fullscreen.

import { button, card, p, render } from "../ui/dom";

export async function fullscreenScreen(
  root: HTMLElement,
  requireFullscreen: boolean,
  doc: Document = document,
): Promise<void> {
  if (!requireFullscreen || doc.fullscreenElement !== null) return;
  const message = p("", "error");
  for (;;) {
    const entered = await new Promise<boolean>((resolve) => {
      render(
        root,
        card(
          "Fullscreen",
          p("The study runs in fullscreen. Press the button to continue."),
          message,
          button("Enter fullscreen", () => {
            doc.documentElement.requestFullscreen().then(
              () => resolve(true),
              (error: unknown) => {
                message.textContent = `Fullscreen was refused (${error instanceof Error ? error.message : String(error)}). Please try again.`;
                resolve(false);
              },
            );
          }),
        ),
      );
    });
    if (entered) return;
  }
}
