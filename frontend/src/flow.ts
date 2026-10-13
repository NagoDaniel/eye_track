// The screen state machine (PLAN §5.2): runs the screens in order. Any failure ends on the error screen.

import { Api } from "./api";
import { errorScreen } from "./screens/error";
import { parseRoute } from "./route";
import { card, p, render } from "./ui/dom";

export async function runApp(root: HTMLElement, api: Api): Promise<never> {
  try {
    const route = parseRoute(location.pathname, location.search);
    const study = await api.getStudy(route.studyCode);
    render(root, card(study.name, p("Loading ...")));
    return await errorScreen(root, new Error("Not implemented yet"), null);
  } catch (e) {
    return errorScreen(root, e, null);
  }
}
