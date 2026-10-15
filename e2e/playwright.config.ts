import { defineConfig } from "@playwright/test";

// The stack is started by scripts/e2e.sh, which passes its address and the admin login in the
// environment. The participant flow has no camera in this milestone, so no fake-device flags yet.
const baseURL = process.env.E2E_BASE_URL;
if (baseURL === undefined) throw new Error("E2E_BASE_URL is not set; run `make e2e`");

export default defineConfig({
  testDir: "tests",
  workers: 1, // the tests share one server and one admin login
  timeout: 120_000,
  reporter: [["list"]],
  use: {
    baseURL,
    ignoreHTTPSErrors: true, // TLS_MODE=internal uses Caddy's local CA
    viewport: { width: 1280, height: 720 },
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
