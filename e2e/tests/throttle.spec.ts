// Playback and tracking on a slow machine (PLAN §14, milestone 6: "playback must not drop frames").
//
// Runs only with E2E_THROTTLE="1,4,6" (`make e2e-throttle`). For each factor the browser's CPU is
// slowed down by that factor (Chrome DevTools protocol, the same as "CPU throttling" in DevTools),
// a whole session is played with a 20 s 720p video, and the numbers that show whether the computer
// kept up are printed. Nothing here fails a run because of a number (DECISIONS 2026-10-25): dropped
// frames are flagged in the quality report, and what this prints is for the researcher to read.
// The fake camera cannot look at the dots, so tracking accuracy is not measured, only load.

import { expect, request as playwrightRequest, test } from "@playwright/test";

import { type Admin, createStudy, login, settledSessionsOf, uploadVideo } from "./admin";
import { runSession } from "./participant";

const FACTORS = (process.env.E2E_THROTTLE ?? "")
  .split(",")
  .map((x) => Number(x.trim()))
  .filter((x) => x >= 1);

test.skip(FACTORS.length === 0, "set E2E_THROTTLE (e.g. 1,4,6), or run `make e2e-throttle`");

interface Quality {
  webcam: { fps_mean: number | null; gaps: number; max_gap_ms: number | null };
  events: { stalls: number; stalls_at_start: number; page_left: number };
  face_lost: { all: { episodes: number } };
  stimulus_frames: { per_video: Record<string, { presented: number; dropped: number; dropped_fraction: number }>; flagged: boolean } | null;
  playback: { frames: number; valid_fraction: number | null } | null;
}

let admin: Admin;
let videoId: string;

test.beforeAll(async ({ baseURL }) => {
  const context = await playwrightRequest.newContext({ baseURL, ignoreHTTPSErrors: true });
  admin = await login(context);
  videoId = (await uploadVideo(admin, "Throttle clip")).id;
});

for (const factor of FACTORS) {
  test(`a session with the CPU ${factor} times slower`, async ({ page, context }) => {
    test.setTimeout(600_000);
    const code = `thr-${String(factor).replace(".", "_")}`;
    const studyId = await createStudy(admin, code, videoId);
    const client = await context.newCDPSession(page);
    await client.send("Emulation.setCPUThrottlingRate", { rate: factor });
    const started = Date.now();
    await page.goto(`/s/${code}?p=ab03i15`);
    await runSession(page, "Thank you", 400_000);
    const seconds = (Date.now() - started) / 1000;
    const [session, ...others] = await settledSessionsOf(admin, studyId);
    expect(others).toEqual([]);
    expect(session?.status).toBe("processed");
    const quality = session?.quality as unknown as Quality;
    const video = Object.values(quality.stimulus_frames?.per_video ?? {})[0];
    console.log(
      "THROTTLE_RESULT " +
        JSON.stringify({
          cpu_slowdown: factor,
          session_seconds: Math.round(seconds),
          video_frames_presented: video?.presented ?? null,
          video_frames_dropped: video?.dropped ?? null,
          dropped_share: video === undefined ? null : Number(video.dropped_fraction.toFixed(3)),
          flagged: quality.stimulus_frames?.flagged ?? null,
          stalls_in_playback: quality.events.stalls,
          webcam_fps: quality.webcam.fps_mean === null ? null : Number(quality.webcam.fps_mean.toFixed(1)),
          webcam_gaps_over_100ms: quality.webcam.gaps,
          face_lost_episodes: quality.face_lost.all.episodes,
          playback_valid_share: quality.playback?.valid_fraction ?? null,
        }),
    );
  });
}
