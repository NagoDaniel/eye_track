// PLAN §14, milestone 1b "done when": a full dummy session can be completed and its events appear
// on the server. Runs a real Chromium against the real stack (proxy, api, database, files).

import { expect, request as playwrightRequest, test } from "@playwright/test";

import { createHash } from "node:crypto";

import { type Admin, createStudy, login, sessionsOf, uploadVideo } from "./admin";
import { runSession } from "./participant";

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
  expect(session.flags).toEqual([]);
  expect(session.events_error).toBeNull();
  for (const [stream, info] of Object.entries(session.streams)) {
    expect(info.missing, `gaps in ${stream}`).toEqual([]);
  }
  expect(session.chunks.events).toBeGreaterThan(0);

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
  expect(session?.flags).toEqual([]);
  for (const info of Object.values(session?.streams ?? {})) expect(info.missing).toEqual([]);
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
