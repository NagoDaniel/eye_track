import { describe, expect, it } from "vitest";

import { SkipBus } from "./skip";

describe("SkipBus", () => {
  it("calls every listener once per fire and reports how many", () => {
    const bus = new SkipBus();
    const calls: string[] = [];
    bus.onSkip(() => calls.push("a"));
    bus.onSkip(() => calls.push("b"));
    expect(bus.fire()).toBe(2);
    expect(calls).toEqual(["a", "b"]);
  });

  it("an unsubscribed listener is not called, and fire with nobody listening is harmless", () => {
    const bus = new SkipBus();
    const calls: string[] = [];
    const off = bus.onSkip(() => calls.push("a"));
    off();
    expect(bus.fire()).toBe(0);
    expect(calls).toEqual([]);
  });

  it("a listener that unsubscribes itself while firing does not break the others", () => {
    const bus = new SkipBus();
    const calls: string[] = [];
    const off = bus.onSkip(() => {
      calls.push("a");
      off();
    });
    bus.onSkip(() => calls.push("b"));
    bus.fire();
    bus.fire();
    expect(calls).toEqual(["a", "b", "b"]);
  });
});
