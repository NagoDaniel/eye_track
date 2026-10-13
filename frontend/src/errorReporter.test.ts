// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

import type { ClientErrorRequest } from "./config";
import { createErrorReporter } from "./errorReporter";
import { EventLog, type EventRecord } from "./eventLog";

function setup(sessionId: string | null = "sid", withLog = true) {
  const posted: ClientErrorRequest[] = [];
  const records: EventRecord[] = [];
  const api = {
    postClientError: vi.fn((request: ClientErrorRequest) => {
      posted.push(request);
      return Promise.resolve();
    }),
  };
  const log = new EventLog((r) => records.push(r), () => 42);
  const reporter = createErrorReporter({
    api,
    getLog: () => (withLog ? log : null),
    getSessionId: () => sessionId,
    getUrl: () => "https://example.org/s/phys1",
    clock: () => 99,
  });
  return { reporter, posted, records, api };
}

describe("error reporter", () => {
  it("logs a client_error event and posts it immediately with the session id", () => {
    const { reporter, posted, records } = setup();
    reporter.report(new TypeError("x is undefined"));
    expect(records).toHaveLength(1);
    expect(records[0]?.type).toBe("client_error");
    expect(records[0]?.data).toMatchObject({ message: "TypeError: x is undefined", url: "https://example.org/s/phys1" });
    expect(typeof records[0]?.data["stack"]).toBe("string");
    expect(posted).toEqual([
      {
        message: "TypeError: x is undefined",
        stack: expect.any(String) as string,
        url: "https://example.org/s/phys1",
        t: 99,
        session_id: "sid",
      },
    ]);
  });

  it("still posts when there is no session or event log yet", () => {
    const { reporter, posted, records } = setup(null, false);
    reporter.report("plain string");
    expect(records).toEqual([]);
    expect(posted[0]).toMatchObject({ message: "plain string", stack: null, session_id: null });
  });

  it("catches window errors and unhandled rejections once installed, and stops after removal", () => {
    const { reporter, posted } = setup();
    const remove = reporter.install(window);
    window.dispatchEvent(new ErrorEvent("error", { error: new Error("boom"), message: "boom" }));
    window.dispatchEvent(Object.assign(new Event("unhandledrejection"), { reason: new Error("rejected") }));
    expect(posted.map((p) => p.message)).toEqual(["Error: boom", "Error: rejected"]);
    remove();
    const swallow = (event: Event): void => event.preventDefault(); // keep the test runner from seeing it
    window.addEventListener("error", swallow);
    window.dispatchEvent(new ErrorEvent("error", { error: new Error("later"), cancelable: true }));
    window.removeEventListener("error", swallow);
    expect(posted).toHaveLength(2);
  });

  it("does not throw when the report itself cannot be sent", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const reporter = createErrorReporter({
      api: { postClientError: () => Promise.reject(new Error("offline")) },
      getLog: () => null,
      getSessionId: () => null,
      getUrl: () => "u",
    });
    expect(() => reporter.report(new Error("x"))).not.toThrow();
    await vi.waitFor(() => expect(errors).toHaveBeenCalled());
    errors.mockRestore();
  });
});
