// PLAN §9, §14 milestone 4: after a session is processed, the admin starts an analysis of the video,
// waits for the worker, looks at the run and downloads the ZIP. A real Chromium and the real stack.
// One participant only: the heatmaps and AOI metrics are made, the synchrony is empty (it needs two).

import { expect, request as playwrightRequest, test } from "@playwright/test";

import { type Admin, createStudy, login, settledSessionsOf, uploadVideo } from "./admin";
import { runSession } from "./participant";

const PARTICIPANT = "AB0315";
// The fake camera cannot follow the dots, so the session's calibration is poor: these thresholds let
// it into the analysis (the plan's defaults are for real participants).
const LOOSE_INCLUSION = { max_validation_error_norm: 100, min_valid_fraction: 0 };
const WHOLE_FRAME = {
  segments: [{ id: "first", start: 0, end: 1.5 }],
  aois: [
    { id: "frame", label: "Whole frame", start: 0, end: 3, shape: "rect", coords: [-1, -1, 2, 2] },
  ],
};

let admin: Admin;
let videoId: string;

test.beforeAll(async ({ baseURL }) => {
  const context = await playwrightRequest.newContext({ baseURL, ignoreHTTPSErrors: true });
  admin = await login(context);
  videoId = (await uploadVideo(admin, "Analysis clip")).id;
  const saved = await admin.request.put(`/admin/api/videos/${videoId}`, {
    headers: admin.headers,
    data: { title: "Analysis clip", aois: WHOLE_FRAME },
  });
  expect(saved.status(), await saved.text()).toBe(200);
});

interface RunRow {
  run_id: string;
  status: string;
  error: string | null;
  included: number;
  excluded: number;
}

test("an analysis is started in admin, runs in the worker and is downloaded as a ZIP", async ({ page }) => {
  const studyId = await createStudy(admin, "e2e-analysis", videoId, { inclusion: LOOSE_INCLUSION });
  await page.goto(`/s/e2e-analysis?p=${PARTICIPANT}`);
  await runSession(page);
  const [session, ...others] = await settledSessionsOf(admin, studyId);
  expect(others).toEqual([]);
  expect(session?.status).toBe("processed");
  const inclusion = (session?.quality as { inclusion: { included: boolean; reasons: string[] } }).inclusion;
  expect(inclusion.included, inclusion.reasons.join(", ")).toBe(true);

  // Start it from the page, like the researcher does.
  await login(page.request);
  await page.goto("/admin/analysis");
  await page.getByLabel("Study").selectOption(String(studyId));
  await page.getByLabel("Video").selectOption(videoId);
  await page.getByRole("button", { name: "Start analysis" }).click();
  await expect(page.locator("p.ok")).toContainText("Queued");

  // The worker takes it from the queue; wait for the run to end.
  const finished = async (): Promise<RunRow | undefined> => {
    const runs = (await (await admin.request.get(`/admin/api/analysis?study_id=${studyId}`)).json()) as RunRow[];
    return runs.find((r) => r.status === "done" || r.status === "failed");
  };
  await expect.poll(async () => (await finished())?.status, { timeout: 120_000, intervals: [2000] }).toBeDefined();
  const run = (await finished())!;
  expect(run.status, run.error ?? "").toBe("done");
  expect(run.included).toBe(1);

  // The run page shows what it made: the heatmap video, the picture of the segment, the synchrony
  // plot, the AOI metrics.
  const base = `/admin/analysis/${studyId}/${videoId}/${run.run_id}`;
  await page.goto(base);
  await expect(page.locator("video")).toHaveAttribute("src", `${base}/files/heatmap.mp4`);
  await expect(page.locator("img.plot")).toHaveCount(2); // one segment, the synchrony plot
  await expect(page.getByRole("heading", { name: "AOI metrics" })).toBeVisible();
  await expect(page.locator("table").last()).toContainText("dwell_sample_s");
  await expect(page.locator("table").last()).toContainText(PARTICIPANT);
  for (const src of await page.locator("img.plot").evaluateAll((e) => e.map((i) => i.getAttribute("src") ?? ""))) {
    expect((await admin.request.get(src)).status(), src).toBe(200);
  }
  const heat = await admin.request.get(`${base}/files/heatmap.mp4`, { headers: { Range: "bytes=0-99" } });
  expect(heat.status()).toBe(206);

  // The ZIP has run.json and every output.
  const zip = await admin.request.get(`${base}/download.zip`);
  expect(zip.status()).toBe(200);
  const body = await zip.body();
  expect(body.subarray(0, 2).toString()).toBe("PK");
  const record = (await (await admin.request.get(`/admin/api/analysis/${studyId}/${videoId}/${run.run_id}`)).json()) as {
    outputs: string[];
    included: { participant_code: string }[];
  };
  expect(record.included.map((i) => i.participant_code)).toEqual([PARTICIPANT]);
  expect(record.outputs).toEqual(
    expect.arrayContaining(["heatmap.mp4", "density.npz", "static/001_first.png", "aoi_metrics_wide.csv", "synchrony.png"]),
  );
  for (const name of record.outputs) {
    expect(body.includes(Buffer.from(`${run.run_id}/${name}`)), name).toBe(true); // entry names are stored plain
  }

  // The participant's page shows the result of the run.
  const participant = await (await admin.request.get(`/admin/participants/${studyId}/${PARTICIPANT}`)).text();
  expect(participant).toContain("<h2>Analysis</h2>");
  expect(participant).toContain(run.run_id);
});
