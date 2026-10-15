// Handles every session left unfinished in IndexedDB (PLAN §5.8): ask, resume or discard, one by one.

import type { StoredSession } from "../upload/idb";
import type { ResumeChoice } from "./resume";

export interface ResumeIo {
  pendingChunks: (session: StoredSession) => Promise<number>;
  ask: (session: StoredSession, pending: number, problem: string | null) => Promise<ResumeChoice>;
  /** Uploads what is left of the session and ends it as interrupted. Throws if that fails. */
  resume: (session: StoredSession) => Promise<void>;
  discard: (session: StoredSession) => Promise<void>;
}

/**
 * Walks through the sessions in order. A resume that fails asks again, with the reason and the
 * option to discard, so a session that can never be uploaded (a token older than 24 h) cannot trap
 * the participant. Returns how many sessions were uploaded.
 */
export async function resumeUnfinished(sessions: StoredSession[], io: ResumeIo): Promise<number> {
  let uploaded = 0;
  for (const session of sessions) {
    let problem: string | null = null;
    for (;;) {
      const choice = await io.ask(session, await io.pendingChunks(session), problem);
      if (choice === "discard") {
        await io.discard(session);
        break;
      }
      try {
        await io.resume(session);
        uploaded += 1;
        break;
      } catch (error) {
        problem = error instanceof Error ? error.message : String(error);
      }
    }
  }
  return uploaded;
}
