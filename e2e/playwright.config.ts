import { defineConfig } from "@playwright/test";

// The stack is started by scripts/e2e.sh, which passes its address, the admin login and the face
// clip (a .y4m made from e2e/fixtures/face.mp4) in the environment. Chromium gets a fake camera that
// plays that clip (PLAN §13); `--use-fake-ui-for-media-stream` accepts the permission prompt.
const baseURL = process.env.E2E_BASE_URL;
if (baseURL === undefined) throw new Error("E2E_BASE_URL is not set; run `make e2e`");
const faceClip = process.env.E2E_FACE_Y4M;
if (faceClip === undefined) throw new Error("E2E_FACE_Y4M is not set; run `make e2e`");

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
    permissions: ["camera"],
    launchOptions: {
      args: [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        `--use-file-for-fake-video-capture=${faceClip}`,
      ],
    },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
