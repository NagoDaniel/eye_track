// PLAN §14, milestone 2 "done when": the E2E test produces complete raw data for a session: webcam
// recording, face features, stimulus frames and events. Runs a real Chromium with a fake camera that
// plays a recorded face (PLAN §13) against the real stack (proxy, api, database, files).

import { expect, request as playwrightRequest, test } from "@playwright/test";

import { createHash } from "node:crypto";

import { type Admin, createStudy, login, sessionsOf, uploadVideo } from "./admin";
import { runSession, runUntilDot } from "./participant";

const PARTICIPANT = "AB0315";
let admin: Admin;
let videoId: string;
let videoSha: string;

test.beforeAll(async ({ baseURL }) => {
  const context = await playwrightRequest.newContext({ baseURL, ignoreHTTPSErrors: true });
  admin = await login(context);
  const video = await uploadVideo(admin, "E2E clip");
  videoId = video.id;
  videoSha = video.sha256;
});

test("the admin can play the uploaded video", async () => {
  const page = await admin.request.get(`/admin/videos/${videoId}`);
  expect(page.status()).toBe(200);
  expect(await page.text()).toContain(`src="/media/videos/${videoId}.mp4"`);
  // Through the proxy: range requests work and the bytes are exactly what was uploaded.
  const part = await admin.request.get(`/media/videos/${videoId}.mp4`, { headers: { Range: "bytes=0-99" } });
  expect(part.status()).toBe(206);
  expect(part.headers()["content-range"]).toMatch(/^bytes 0-99\//);
  const whole = await admin.request.get(`/media/videos/${videoId}.mp4`);
  expect(createHash("sha256").update(await whole.body()).digest("hex")).toBe(videoSha);
});

const STREAMS = ["webcam", "features", "events", "stimulus_frames"] as const;
// The fake camera cannot look where the dots are, so the browser gate fails and the server flags
// the session; no other flag is expected in a normal run.
const GATE_FLAG = "calibration_gate_failed";
const otherFlags = (flags: string[]): string[] => flags.filter((f) => f !== GATE_FLAG);

function indexOfType(types: string[], type: string, from = 0): number {
  const i = types.indexOf(type, from);
  expect(i, `event ${type} after position ${from}`).toBeGreaterThanOrEqual(from);
  return i;
}

test("a full dummy session is completed and its events reach the server", async ({ page }) => {
  const studyId = await createStudy(admin, "e2e-full", videoId);
  await page.goto(`/s/e2e-full?p=${PARTICIPANT}`);
  await runSession(page);

  const [session, ...others] = await sessionsOf(admin, studyId);
  expect(others).toEqual([]);
  expect(session).toBeDefined();
  if (session === undefined) return;
  expect(session.status).toBe("uploaded");
  expect(otherFlags(session.flags)).toEqual([]);
  expect(session.events_error).toBeNull();
  // Complete raw data: every stream has chunks and none has a gap (PLAN §14).
  for (const stream of STREAMS) {
    expect(session.streams[stream]?.missing, `gaps in ${stream}`).toEqual([]);
    expect(session.chunks[stream], `chunks of ${stream}`).toBeGreaterThan(0);
  }

  const types = session.events.map((e) => e.type);
  const start = indexOfType(types, "session_start");
  const firstDot = indexOfType(types, "calib_dot_show", start);
  const load = indexOfType(types, "video_load", firstDot);
  const play = indexOfType(types, "video_play", load);
  indexOfType(types, "video_ended", play);

  // 13 calibration dots; validation before the video and after the last one: 5 dots each.
  const clicks = (phase: string): number =>
    session.events.filter((e) => e.type === "calib_dot_click" && e.data.phase === phase).length;
  expect(clicks("calibration")).toBe(13);
  expect(clicks("validation")).toBe(10);

  // The participant's results page shows the same session with its summary.
  const results = await admin.request.get(`/admin/participants/${studyId}/${PARTICIPANT}`);
  expect(results.status()).toBe(200);
  const html = await results.text();
  expect(html).toContain(session.id.slice(0, 8));
  expect(html).toContain("13 clicked of 13 shown");
  expect(html).toContain("10 clicked of 10 shown");

  // The recorder runs from the fullscreen step to the end of the last validation.
  // (its `start` event fires a moment after the first dot is shown, so compare with the first click)
  const recorderStart = indexOfType(types, "recorder_start", start);
  expect(recorderStart).toBeLessThan(indexOfType(types, "calib_dot_click", firstDot));
  const recorderStop = indexOfType(types, "recorder_stop", play);
  const startData = session.events[recorderStart]?.data ?? {};
  expect(String(startData.mime_type)).toContain("video/webm");
  expect((startData.settings as { width: number }).width).toBeGreaterThanOrEqual(640);
  const stopData = session.events[recorderStop]?.data ?? {};
  expect(stopData.chunks).toBe(session.chunks.webcam);
  expect(types.filter((t) => t === "recorder_dataavailable")).toHaveLength(session.chunks.webcam ?? -1);

  // The browser gate is logged for the pre-video block and for the final validation; the server
  // flag is derived from the pre-video result (DECISIONS milestone 2, T2).
  const gates = session.events.filter((e) => e.type === "calib_gate_result");
  expect(gates.map((e) => e.data.block)).toEqual(["before_video", "after_last_video"]);
  expect(session.flags.includes(GATE_FLAG)).toBe(gates[0]?.data.passed === false);

  const times = session.events.map((e) => e.t);
  expect(times).toEqual([...times].sort((a, b) => a - b));
  expect(session.events.find((e) => e.type === "session_start")?.data.client_info).toBeDefined();
});

test("uploads survive a dropped connection", async ({ page }) => {
  const studyId = await createStudy(admin, "e2e-drop", videoId);
  await page.goto(`/s/e2e-drop?p=${PARTICIPANT}`);
  // Every chunk upload fails for a while, then the network comes back.
  let blocked = true;
  await page.route("**/api/sessions/*/chunks/**", (route) =>
    blocked ? route.abort("connectionreset") : route.continue(),
  );
  setTimeout(() => {
    blocked = false;
  }, 8_000);
  await runSession(page);

  const [session] = await sessionsOf(admin, studyId);
  expect(session?.status).toBe("uploaded");
  expect(otherFlags(session?.flags ?? [])).toEqual([]);
  for (const info of Object.values(session?.streams ?? {})) expect(info.missing).toEqual([]);
  for (const stream of STREAMS) expect(session?.chunks[stream]).toBeGreaterThan(0);
});

test("a reloaded session can resume its upload and is flagged interrupted", async ({ page }) => {
  const studyId = await createStudy(admin, "e2e-reload", videoId);
  await page.goto(`/s/e2e-reload?p=${PARTICIPANT}`);
  // Chunks stay in IndexedDB because the server never gets them before the reload.
  await page.route("**/api/sessions/*/chunks/**", (route) => route.abort("connectionreset"));
  await runSession(page, "Before you start"); // consent given, session running
  await page.waitForTimeout(3_000); // at least one events chunk (chunk_ms 2000) is queued
  await page.unroute("**/api/sessions/*/chunks/**");
  await page.reload();
  await page.getByRole("button", { name: "Resume upload" }).click();
  await expect(page.getByRole("heading", { name: "Thank you" })).toBeVisible({ timeout: 30_000 });

  const [session] = await sessionsOf(admin, studyId);
  expect(session?.status).toBe("uploaded");
  expect(session?.flags).toContain("interrupted");
  expect(session?.events_error).toBeNull();
  expect(session?.events_total).toBeGreaterThan(0);
});

test("a reload while the webcam chunks wait in the browser still ends with the full recording", async ({
  page,
}) => {
  const studyId = await createStudy(admin, "e2e-reload-webcam", videoId);
  await page.goto(`/s/e2e-reload-webcam?p=${PARTICIPANT}`);
  // Nothing reaches the server during the session: every stream stays in IndexedDB.
  await page.route("**/api/sessions/*/chunks/**", (route) => route.abort("connectionreset"));
  await runSession(page, "Uploading your recording");
  await page.waitForTimeout(1_500); // the last buffers are written to IndexedDB
  await page.unroute("**/api/sessions/*/chunks/**");
  await page.reload();
  await page.getByRole("button", { name: "Resume upload" }).click();
  await expect(page.getByRole("heading", { name: "Thank you" })).toBeVisible({ timeout: 60_000 });

  const [session] = await sessionsOf(admin, studyId);
  expect(session?.status).toBe("uploaded");
  expect(session?.flags).toContain("interrupted");
  for (const stream of STREAMS) {
    expect(session?.streams[stream]?.missing, `gaps in ${stream}`).toEqual([]);
    expect(session?.chunks[stream], `chunks of ${stream}`).toBeGreaterThan(0);
  }
  const types = session?.events.map((e) => e.type) ?? [];
  expect(types).toContain("recorder_stop");
});

test("debug mode: the overlay shows, S skips the calibration, the session is a debug session", async ({
  page,
}) => {
  const studyId = await createStudy(admin, "e2e-debug", videoId, { debug_allowed: true });
  await page.goto(`/s/e2e-debug?p=${PARTICIPANT}&debug=1`);
  // Click through to the first calibration dot, then skip the calibration with S (PLAN §12.1).
  await runSession(page, "Before you start");
  await expect(page.locator(".debug-panel")).toContainText("DEBUG");
  // The panel covers the right part of the screen, where dots appear: hide it with D, as a person would.
  await page.keyboard.press("d");
  await expect(page.locator(".debug-root")).toBeHidden();
  await runUntilDot(page);
  await page.keyboard.press("s");
  await runSession(page);

  const [session] = await sessionsOf(admin, studyId);
  expect(session?.status).toBe("uploaded");
  expect(session?.is_debug).toBe(true);
  const skips = session?.events.filter((e) => e.type === "debug_skip") ?? [];
  expect(skips.map((e) => e.data.target)).toEqual(["calibration"]);
  const clicked = session?.events.filter(
    (e) => e.type === "calib_dot_click" && e.data.phase === "calibration",
  );
  expect(clicked?.length ?? 99).toBeLessThan(13);
});

test("debug mode is refused for a study that does not allow it", async ({ page }) => {
  await createStudy(admin, "e2e-nodebug", videoId);
  await page.goto(`/s/e2e-nodebug?p=${PARTICIPANT}&debug=1`);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByText(/debug mode is not allowed/i)).toBeVisible();
});
