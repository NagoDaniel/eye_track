// Admin side of the end-to-end test: logs in over HTTP like a browser would and uses the JSON API.

import { readFileSync } from "node:fs";

import { type APIRequestContext, expect } from "@playwright/test";

function env(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") throw new Error(`${name} is not set; run \`make e2e\``);
  return value;
}

function csrfFrom(html: string): string {
  const match = /name="csrf_token" value="([^"]+)"/.exec(html);
  if (match?.[1] === undefined) throw new Error("no csrf_token field in the page");
  return match[1];
}

export interface Admin {
  request: APIRequestContext;
  headers: Record<string, string>;
}

export async function login(request: APIRequestContext): Promise<Admin> {
  const form = await request.get("/admin/login");
  expect(form.status()).toBe(200);
  const response = await request.post("/admin/login", {
    form: {
      username: env("E2E_ADMIN_USER"),
      password: env("E2E_ADMIN_PASSWORD"),
      csrf_token: csrfFrom(await form.text()),
    },
    maxRedirects: 0,
  });
  expect(response.status()).toBe(303);
  const home = await request.get("/admin/");
  expect(home.status()).toBe(200);
  return { request, headers: { "X-CSRF-Token": csrfFrom(await home.text()) } };
}

export interface UploadedVideo {
  id: string;
  sha256: string;
}

export async function uploadVideo(admin: Admin, title: string): Promise<UploadedVideo> {
  const path = env("E2E_VIDEO");
  const response = await admin.request.post("/admin/api/videos", {
    headers: admin.headers,
    multipart: {
      title,
      file: { name: "clip.mp4", mimeType: "video/mp4", buffer: readFileSync(path) },
    },
  });
  expect(response.status(), await response.text()).toBe(201);
  const body = (await response.json()) as { id: number; sha256: string };
  return { id: String(body.id), sha256: body.sha256 };
}

/**
 * The fake camera plays a still-ish clip of a face that cannot follow the calibration dots, so the
 * browser gate fails: one attempt only (`max_attempts` 1) keeps the test short, and the session
 * continues flagged (`continue_flagged`, the plan default).
 */
export async function createStudy(admin: Admin, code: string, videoId: string): Promise<number> {
  const response = await admin.request.post("/admin/api/studies", {
    headers: admin.headers,
    data: {
      name: `E2E study ${code}`,
      code,
      mode: "remote",
      // dot_duration_ms is short so that the test is fast; everything else is the plan default.
      config: {
        playlist: [videoId],
        calibration: { dot_duration_ms: 700 },
        calibration_gate: { max_attempts: 1 },
      },
    },
  });
  expect(response.status(), await response.text()).toBe(201);
  const body = (await response.json()) as { id: number };
  return body.id;
}

export interface SessionEvent {
  t: number;
  type: string;
  data: Record<string, unknown>;
}

export interface SessionDetail {
  id: string;
  status: string;
  flags: string[];
  chunks: Record<string, number>;
  streams: Record<string, { missing: number[]; count: number }>;
  events: SessionEvent[];
  events_total: number;
  events_error: string | null;
}

export async function sessionsOf(admin: Admin, studyId: number): Promise<SessionDetail[]> {
  const list = await admin.request.get(`/admin/api/sessions?study_id=${studyId}`);
  expect(list.status()).toBe(200);
  const rows = (await list.json()) as { id: string }[];
  const details: SessionDetail[] = [];
  for (const row of rows) {
    const detail = await admin.request.get(`/admin/api/sessions/${row.id}`);
    expect(detail.status()).toBe(200);
    details.push((await detail.json()) as SessionDetail);
  }
  return details;
}
