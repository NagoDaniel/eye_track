// The URL carries the study code (`/s/<code>`) and optionally the participant code (`?p=`), PLAN §5.2 step 1.

import { PARTICIPANT_CODE_QUERY_PARAM, STUDY_PATH_PREFIX } from "./constants";

export interface Route {
  studyCode: string;
  participantCode: string | null;
}

/** Throws when the path is not `/s/<code>`. */
export function parseRoute(pathname: string, search: string): Route {
  if (!pathname.startsWith(STUDY_PATH_PREFIX)) {
    throw new Error("This link does not contain a study code. Please use the link you were given.");
  }
  const studyCode = decodeURIComponent(pathname.slice(STUDY_PATH_PREFIX.length).replace(/\/+$/, ""));
  if (studyCode === "" || studyCode.includes("/")) {
    throw new Error("This link does not contain a valid study code. Please use the link you were given.");
  }
  const participantCode = new URLSearchParams(search).get(PARTICIPANT_CODE_QUERY_PARAM);
  return { studyCode, participantCode: participantCode === "" ? null : participantCode };
}
