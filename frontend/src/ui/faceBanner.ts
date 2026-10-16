// Non-blocking banner shown while the face has been missing for a while (PLAN §5.2): it asks the
// participant to look at the screen, and never pauses or blocks anything (the dropout is data).

import { el } from "./dom";

export const FACE_BANNER_TEXT = "We cannot see your face. Please look at the screen.";

export interface FaceBanner {
  setVisible(visible: boolean): void;
  destroy(): void;
}

export function createFaceBanner(parent: HTMLElement = document.body): FaceBanner {
  const banner = el("div", {
    class: "face-banner",
    text: FACE_BANNER_TEXT,
    attrs: { role: "status" },
  });
  banner.hidden = true;
  parent.append(banner);
  return {
    setVisible: (visible) => {
      banner.hidden = !visible;
    },
    destroy: () => banner.remove(),
  };
}
