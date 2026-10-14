import { describe, expect, it } from "vitest";

import type { StoredSession } from "../upload/idb";
import type { ResumeChoice } from "./resume";
import { resumeUnfinished } from "./resumeFlow";

const session = (id: string): StoredSession => ({ sessionId: id, studyCode: "s" }) as StoredSession;

function io(choices: ResumeChoice[], failures: Record<string, number> = {}) {
  const log: string[] = [];
  const left = { ...failures };
  return {
    log,
    io: {
      pendingChunks: () => Promise.resolve(2),
      ask: (s: StoredSession, _n: number, problem: string | null) => {
        log.push(`ask ${s.sessionId} ${problem ?? "-"}`);
        return Promise.resolve(choices.shift() ?? "discard");
      },
      resume: (s: StoredSession) => {
        if ((left[s.sessionId] ?? 0) > 0) {
          left[s.sessionId] = (left[s.sessionId] ?? 0) - 1;
          log.push(`fail ${s.sessionId}`);
          return Promise.reject(new Error("401 expired"));
        }
        log.push(`resume ${s.sessionId}`);
        return Promise.resolve();
      },
      discard: (s: StoredSession) => {
        log.push(`discard ${s.sessionId}`);
        return Promise.resolve();
      },
    },
  };
}

describe("resumeUnfinished", () => {
  it("handles every unfinished session, not only the first", async () => {
    const { io: x, log } = io(["resume", "discard", "resume"]);
    const n = await resumeUnfinished([session("a"), session("b"), session("c")], x);
    expect(n).toBe(2);
    expect(log).toEqual(["ask a -", "resume a", "ask b -", "discard b", "ask c -", "resume c"]);
  });

  it("asks again with the reason after a failed resume, and Discard then works", async () => {
    const { io: x, log } = io(["resume", "discard"], { a: 1 });
    const n = await resumeUnfinished([session("a")], x);
    expect(n).toBe(0);
    expect(log).toEqual(["ask a -", "fail a", "ask a 401 expired", "discard a"]);
  });

  it("a retry that works counts as uploaded", async () => {
    const { io: x } = io(["resume", "resume"], { a: 1 });
    expect(await resumeUnfinished([session("a")], x)).toBe(1);
  });

  it("does nothing without sessions", async () => {
    expect(await resumeUnfinished([], io([]).io)).toBe(0);
  });
});
