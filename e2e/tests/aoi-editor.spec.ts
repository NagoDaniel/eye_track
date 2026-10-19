// PLAN §9.3: the admin draws areas of interest on the video instead of typing coordinates. A real
// Chromium drives the editor on the video page; what is saved is checked through the JSON API.

import { expect, request as playwrightRequest, test } from "@playwright/test";

import { type Admin, login, uploadVideo } from "./admin";

let admin: Admin;
let videoId: string;

test.beforeAll(async ({ baseURL }) => {
  const context = await playwrightRequest.newContext({ baseURL, ignoreHTTPSErrors: true });
  admin = await login(context);
  videoId = (await uploadVideo(admin, "AOI editor clip")).id;
});

interface Saved {
  segments: { id: string; start: number; end: number }[];
  aois: { id: string; label: string; start: number; end: number; shape: string; coords: unknown }[];
}

test("areas are drawn, moved and edited on the picture, and saved as aois.json", async ({ page }) => {
  await login(page.request); // the page shares the cookies of its request context
  await page.goto(`/admin/videos/${videoId}`);
  const editor = page.locator("#aoi-editor");
  await expect(editor).toBeVisible();
  await page.waitForFunction(() => {
    const v = document.querySelector<HTMLVideoElement>("#aoi-editor video");
    return v !== null && v.videoHeight > 0;
  });
  const textarea = page.locator('textarea[name="aois"]');
  const text = async (): Promise<Saved> => JSON.parse(await textarea.inputValue()) as Saved;
  // Mouse positions are viewport positions: bring the picture to the top and measure it again.
  let svg = { x: 0, y: 0, width: 1, height: 1 };
  const showPicture = async (): Promise<void> => {
    await editor.locator(".stage").evaluate((e) => e.scrollIntoView({ block: "start" }));
    svg = (await editor.locator("svg").boundingBox())!;
  };
  const at = (fx: number, fy: number): [number, number] => [svg.x + fx * svg.width, svg.y + fy * svg.height];

  // A rectangle: drag from (0.2, 0.2) to (0.6, 0.7).
  await editor.getByRole("button", { name: "Add rectangle" }).click();
  await showPicture();
  await page.mouse.move(...at(0.2, 0.2));
  await page.mouse.down();
  await page.mouse.move(...at(0.4, 0.5), { steps: 4 });
  await page.mouse.move(...at(0.6, 0.7), { steps: 4 });
  await page.mouse.up();
  let saved = await text();
  expect(saved.aois).toHaveLength(1);
  const rect = saved.aois[0]!;
  expect(rect.shape).toBe("rect");
  (rect.coords as number[]).forEach((v, i) => expect(v).toBeCloseTo([0.2, 0.2, 0.6, 0.7][i]!, 1));
  expect([rect.start, rect.end]).toEqual([0, 3]); // the whole video until the researcher changes it

  // A polygon: three clicks, then Finish.
  await editor.getByRole("button", { name: "Add polygon" }).click();
  await showPicture();
  for (const [fx, fy] of [[0.7, 0.1], [0.95, 0.1], [0.8, 0.4]] as const) await page.mouse.click(...at(fx, fy));
  await editor.getByRole("button", { name: "Finish polygon" }).click();
  saved = await text();
  expect(saved.aois).toHaveLength(2);
  expect(saved.aois[1]!.shape).toBe("polygon");
  expect(saved.aois[1]!.coords as number[][]).toHaveLength(3);

  // Drag the rectangle to the right by a tenth of the picture.
  await showPicture();
  await page.mouse.move(...at(0.4, 0.45));
  await page.mouse.down();
  await page.mouse.move(...at(0.45, 0.45), { steps: 3 });
  await page.mouse.move(...at(0.5, 0.45), { steps: 3 });
  await page.mouse.up();
  saved = await text();
  (saved.aois[0]!.coords as number[]).forEach((v, i) => expect(v).toBeCloseTo([0.3, 0.2, 0.7, 0.7][i]!, 1));

  // Name it and set when it exists; add a segment.
  const rows = page.locator("#aoi-table tr[data-index]");
  await expect(rows).toHaveCount(2);
  await rows.nth(0).getByLabel("Area id").fill("panel");
  await rows.nth(0).getByLabel("Area label").fill("Code panel");
  await rows.nth(0).getByLabel("Area ends").fill("2");
  await editor.getByRole("button", { name: "Add segment" }).click();
  await page.locator("#segment-table").getByLabel("Segment ends").fill("1.5");

  // A mistake is pointed out before saving.
  await rows.nth(1).getByLabel("Area starts").fill("5");
  await expect(page.locator("#aoi-problems")).toContainText("must end after it starts");
  await rows.nth(1).getByLabel("Area starts").fill("0");
  await expect(page.locator("#aoi-problems li")).toHaveCount(0);

  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator("p.ok")).toHaveText("Saved.");

  // The server accepted it (it validates the schema) and the editor shows it again after a reload.
  const stored = (await (await admin.request.get(`/admin/api/videos/${videoId}`)).json()) as { aois: Saved };
  expect(stored.aois.aois.map((a) => [a.id, a.label, a.start, a.end, a.shape])).toEqual([
    ["panel", "Code panel", 0, 2, "rect"],
    ["area_2", "Area 2", 0, 3, "polygon"],
  ]);
  expect(stored.aois.segments).toEqual([{ id: "segment_1", start: 0, end: 1.5 }]);
  await page.reload();
  await expect(page.locator("#aoi-table tr[data-index]")).toHaveCount(2);

  // Hand edits in the text box are picked up; broken text pauses the editor instead of losing work.
  await textarea.fill("{ not json");
  await expect(page.locator("#aoi-notice")).toContainText("not valid JSON");
  await textarea.fill(JSON.stringify(stored.aois));
  await expect(page.locator("#aoi-notice")).toBeHidden();
  await expect(page.locator("#aoi-table tr[data-index]")).toHaveCount(2);
});
