// Bundle the face model worker into public/mediapipe/face-worker.js as a CLASSIC worker.
// MediaPipe's WASM loader needs `importScripts`, which module workers do not have, so Vite's own
// worker bundling (module workers) cannot be used. Runs before `vite build` / `vite`.
import { build } from "esbuild";
import { fileURLToPath } from "node:url";

const here = (rel) => fileURLToPath(new URL(rel, import.meta.url));

await build({
  entryPoints: [here("../src/face/modelWorker.ts")],
  outfile: here("../public/mediapipe/face-worker.js"),
  bundle: true,
  format: "iife",
  target: "es2022",
  minify: true,
  logLevel: "warning",
});
