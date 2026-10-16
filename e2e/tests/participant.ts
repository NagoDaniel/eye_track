// Drives the participant app like a person would: reads the page, clicks what is there.

import type { Page } from "@playwright/test";

/**
 * Click through the whole session until the "Thank you" screen. Whatever is on screen decides the
 * action: consent box, a Continue / I understand button, the fullscreen button, or a calibration/validation dot
 * (early clicks on a dot only flash it, so repeating is harmless).
 */
export async function runSession(page: Page, until: string = "Thank you"): Promise<void> {
  const deadline = Date.now() + 100_000;
  while (Date.now() < deadline) {
    if (await page.getByRole("heading", { name: until }).isVisible()) return;
    const box = page.locator("#consent-box");
    if ((await box.count()) > 0 && !(await box.isChecked())) await box.check();
    const dot = page.locator(".dot-hit");
    if ((await dot.count()) > 0) {
      await dot.first().click({ timeout: 1_000 }).catch(() => undefined);
    } else {
      for (const name of ["Continue", "I understand", "Enter fullscreen"]) {
        const button = page.getByRole("button", { name, exact: true });
        if ((await button.count()) > 0 && (await button.isEnabled())) {
          await button.click().catch(() => undefined);
          break;
        }
      }
    }
    await page.waitForTimeout(150);
  }
  throw new Error(`the session did not reach "${until}" in time; page text: ${await page.innerText("body")}`);
}

/** Click through the screens until the first dot of a calibration is on the page (the dot is not clicked). */
export async function runUntilDot(page: Page): Promise<void> {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if ((await page.locator(".dot-hit").count()) > 0) return;
    const box = page.locator("#consent-box");
    if ((await box.count()) > 0 && !(await box.isChecked())) await box.check();
    for (const name of ["Continue", "I understand", "Enter fullscreen"]) {
      const button = page.getByRole("button", { name, exact: true });
      if ((await button.count()) > 0 && (await button.isEnabled())) {
        await button.click().catch(() => undefined);
        break;
      }
    }
    await page.waitForTimeout(150);
  }
  throw new Error(`no dot appeared in time; page text: ${await page.innerText("body")}`);
}
