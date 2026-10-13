// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { installUnloadGuard } from "./unloadGuard";

describe("installUnloadGuard", () => {
  it("asks for confirmation only while something is pending, and can be removed", () => {
    let pending = true;
    const remove = installUnloadGuard(() => pending, window);
    const first = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(first);
    expect(first.defaultPrevented).toBe(true);

    pending = false;
    const second = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(second);
    expect(second.defaultPrevented).toBe(false);

    pending = true;
    remove();
    const third = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(third);
    expect(third.defaultPrevented).toBe(false);
  });
});
