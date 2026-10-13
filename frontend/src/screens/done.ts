// Done screen (PLAN §5.2 step 12). The end of the line.

import { blockForever, card, p, render } from "../ui/dom";

export function doneScreen(root: HTMLElement): Promise<never> {
  render(root, card("Thank you", p("Everything has been uploaded. You can close this page now.")));
  return blockForever();
}
