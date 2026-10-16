import { defineConfig } from "vite";

export default defineConfig({
  build: { outDir: "dist", emptyOutDir: true },
  // The app imports shared/*.json, which sits next to the frontend directory (also in the dev stack).
  server: { fs: { allow: [".."] } },
});
