// Self-host the browser MediaPipe files (PLAN §11.1: no CDN). Runs before `vite build` / `vite`.
// Copies the WASM runtime from node_modules and downloads face_landmarker.task, verifying the
// sha256 from shared/models.json (the single source of truth, also used by the backend image).
import { createHash } from "node:crypto";
import { cp, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const here = (rel) => fileURLToPath(new URL(rel, import.meta.url));
const outDir = here("../public/mediapipe/");
const sha256 = (buf) => createHash("sha256").update(buf).digest("hex");

const manifest = JSON.parse(await readFile(here("../../shared/models.json"), "utf8"));
const model = manifest.face_landmarker;

await mkdir(outDir, { recursive: true });
await cp(here("../node_modules/@mediapipe/tasks-vision/wasm/"), `${outDir}wasm/`, {
  recursive: true,
});

const dest = `${outDir}face_landmarker.task`;
let cached = false;
try {
  cached = sha256(await readFile(dest)) === model.sha256;
} catch {
  /* not downloaded yet */
}
if (!cached) {
  const response = await fetch(model.url);
  if (!response.ok) throw new Error(`face_landmarker: HTTP ${response.status} for ${model.url}`);
  const data = Buffer.from(await response.arrayBuffer());
  if (data.length !== model.bytes) {
    throw new Error(`face_landmarker: size ${data.length} != expected ${model.bytes}`);
  }
  const actual = sha256(data);
  if (actual !== model.sha256) {
    throw new Error(`face_landmarker: sha256 mismatch: got ${actual}, expected ${model.sha256}`);
  }
  await writeFile(`${dest}.tmp`, data);
  await rename(`${dest}.tmp`, dest);
}
await rm(`${dest}.tmp`, { force: true });
console.log("mediapipe assets ready in public/mediapipe");
