// Constants from PLAN that are not part of the per-study config (PLAN §1: no inline literals).
// Values that a researcher can set per study (dot_duration_ms, margin, chunk_ms, ...) are in
// StudyConfig (config.ts) and never repeated here.

// --- Calibration and validation dots (PLAN §5.5) ---
/** A click on a dot is accepted only in the last this-many ms of its shrink duration. */
export const CALIB_CLICK_WINDOW_MS = 600;
/** Outer radius at dot onset (px). */
export const DOT_OUTER_RADIUS_PX = 20;
/** Radius of the dark centre (px); constant. */
export const DOT_CENTRE_RADIUS_PX = 4;
/** Outer radius the dot has shrunk to at the end of `dot_duration_ms` (px). */
export const DOT_SHRUNK_RADIUS_PX = 6;
/** The training window of a dot ends this long after the click, so the dot stays visible that long. */
export const CALIB_POST_CLICK_MS = 100;
/** How long an early click flashes the dot ("wait"). Not in the plan: UI detail. */
export const DOT_EARLY_CLICK_FLASH_MS = 250;
/** Quadrant centres and validation points, as fractions of the viewport. */
export const INNER_POINT_LOW = 0.25;
export const INNER_POINT_HIGH = 0.75;
/** Fixed point counts of the plan; a study config that disagrees is an error (PLAN §5.5). */
export const CALIBRATION_POINT_COUNT = 13;
export const VALIDATION_POINT_COUNT = 5;

// --- Upload queue (PLAN §5.8) ---
export const UPLOAD_BACKOFF_BASE_MS = 500;
export const UPLOAD_BACKOFF_MAX_MS = 30_000;
export const UPLOAD_BACKOFF_FACTOR = 2;
/** Rounds of `/complete` + re-sending missing chunks before giving up loudly. */
export const COMPLETE_MAX_ROUNDS = 5;
export const UPLOAD_DB_NAME = "eyetrack-upload";
export const UPLOAD_DB_VERSION = 1;

// --- Playback (PLAN §5.7) ---
/** A jump of `currentTime` larger than this (s) between two `timeupdate` events is treated as a seek attempt. */
export const SEEK_TOLERANCE_S = 0.5;
export const PAUSE_KEY = " ";

// --- Routing and participant code (PLAN §5.2 step 1; DECISIONS 1b item 11) ---
export const PARTICIPANT_CODE_QUERY_PARAM = "p";
export const STUDY_PATH_PREFIX = "/s/";
