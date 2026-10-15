// Client errors (PLAN §12.1): in all modes, window.onerror / unhandledrejection are logged into the
// events stream and sent to POST /api/client-errors immediately.

import type { Api } from "./api";
import type { EventLog } from "./eventLog";
import { now } from "./clock";

export interface ErrorReporterDeps {
  api: Pick<Api, "postClientError">;
  /** The event log of the running session, or null before there is one. */
  getLog: () => EventLog | null;
  getSessionId: () => string | null;
  getUrl?: () => string;
  clock?: () => number;
}

export interface ErrorReporter {
  report(error: unknown): void;
  /** Listen for uncaught errors and unhandled rejections. Returns a remover. */
  install(target: Window): () => void;
}

export function createErrorReporter(deps: ErrorReporterDeps): ErrorReporter {
  const clock = deps.clock ?? now;
  const getUrl = deps.getUrl ?? (() => location.href);

  function report(error: unknown): void {
    const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    const stack = error instanceof Error ? (error.stack ?? null) : null;
    const url = getUrl();
    try {
      deps.getLog()?.log("client_error", { message, stack, url });
    } catch (e) {
      console.error("could not log client_error", e);
    }
    deps.api
      .postClientError({ message, stack, url, t: clock(), session_id: deps.getSessionId() })
      .catch((e: unknown) => console.error("could not report the client error", e));
  }

  function install(target: Window): () => void {
    const onError = (event: ErrorEvent): void => report(event.error ?? event.message);
    const onRejection = (event: PromiseRejectionEvent): void => report(event.reason);
    target.addEventListener("error", onError);
    target.addEventListener("unhandledrejection", onRejection);
    return () => {
      target.removeEventListener("error", onError);
      target.removeEventListener("unhandledrejection", onRejection);
    };
  }

  return { report, install };
}
