// The root page asks for the study code and sends a student with a valid code to that study's page.
// A real Chromium against the real stack.

import { expect, request as playwrightRequest, test } from "@playwright/test";

import { type Admin, createStudy, login, uploadVideo } from "./admin";

const CODE = "e2e-landing";
let admin: Admin;

test.beforeAll(async ({ baseURL }) => {
  const context = await playwrightRequest.newContext({ baseURL, ignoreHTTPSErrors: true });
  admin = await login(context);
  const video = await uploadVideo(admin, "Landing clip");
  await createStudy(admin, CODE, video.id);
});

test("the root page explains the code and opens the study page for a valid code", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Enter the code of your study")).toBeVisible();

  await page.getByLabel("Study code").fill("no-such-study");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("No study with this code was found")).toBeVisible();
  expect(new URL(page.url()).pathname).toBe("/");

  await page.getByLabel("Study code").fill(CODE);
  await page.getByRole("button", { name: "Continue" }).click();
  await page.waitForURL(`**/s/${CODE}`);
  await expect(page.getByLabel("Participant code")).toBeVisible();
});
