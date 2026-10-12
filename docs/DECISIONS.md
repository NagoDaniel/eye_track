# Decisions and deviations from PLAN.md

Every agreed deviation from `docs/PLAN.md`, and every choice the plan leaves open (library versions, model weights and their sha256, tuned thresholds). Newest first.

Format:

```
## YYYY-MM-DD — <short title>
- Plan section: §x.y
- Decision: what we do instead / what was chosen
- Reason:
- Approved by: <name> (or "plan leaves open")
```

---

## 2026-10-12 — Pipeline B model weights (yakhyo/gaze-estimation ONNX)
- Plan section: §8.5, §11.2
- Decision: use yakhyo/gaze-estimation ONNX weights (L2CS-style, Gaze360-trained), release `weights`, ONNX opset 20, exported from PyTorch 2.8.0.
  - Primary: `resnet50_gaze.onnx`, https://github.com/yakhyo/gaze-estimation/releases/download/weights/resnet50_gaze.onnx, 95,425,874 bytes, sha256 `bb28d421565adc4dfb665742f8fc80bdef36dd8caa0c87e040e0937f9fdca9a6`, Gaze360 MAE 11.34°.
  - Optional CPU fallback: `mobileone_s0_gaze.onnx`, .../mobileone_s0_gaze.onnx, 4,974,521 bytes, sha256 `8b4fdc4e3da44733c9a82e7776b411e4a39f94e8e285aee0fc85a548a55f7d9f`, MAE 12.58°.
  - Not used: `resnet18_gaze.onnx` (45,066,134 bytes, sha256 `404fec1efd07ff49f981e47f461c20c2627119e465ec441bbd1c067d3f16e657`).
  - I/O: input `input` float32 [1,3,448,448], RGB, pixels scaled to 0–1 then ImageNet mean [0.485,0.456,0.406] / std [0.229,0.224,0.225]. Outputs yaw and pitch, each [1,90] logits over bins. Decode: softmax, expected = Σ(p·i) for i=0..89, angle_deg = expected*4 − 180, convert to radians.
  - Crop rule (fixed, must not change between runs): landmark bounding box expanded 20 %, resized to 448×448 (PLAN §8.5). The reference code uses a face-detector crop, so outputs differ slightly; per-session calibration absorbs this.
  - Licence: code MIT; weights trained on Gaze360, non-commercial research use only. Acceptable for this project; note in README (T13).
- Reason: plan leaves weight source open; user supplied URLs and hashes.
- Approved by: Nágó Dániel

## 2026-10-12 — Milestone 1a scope choices
- Plan section: §11.1, §11.4, §3, §14
- Decision: (1) Browser MediaPipe WASM + model are bundled into `dist/` in 1a (T8), not deferred to M2. (2) GPU compose profile / `worker-gpu` deferred to M5. (3) `docs/DATA_FORMATS.md` deferred until the first files are written (M2/M3). (4) Admin JSON API under `/admin/api/*`; everything not `/api`, `/admin`, `/media` served as static SPA with fallback. (5) Admin cookie 12 h, single role, no lockout in 1a. (6) vitest for frontend unit tests. (7) `AdminUser` and worker heartbeat tables added beyond §4. (8) CI runs a health+login smoke test in 1a; Playwright E2E added in M2. (9) `face_landmarker.task` taken from the official Google storage URL; pinned URL and sha256 recorded here when T7 is implemented.
- Reason: plan leaves these open or sequences them later; confirmed in `/milestone-start 1a` review.
- Approved by: Nágó Dániel

## 2026-10-12 — Milestone 1 split into 1a and 1b
- Plan section: §14
- Decision: Milestone 1 is split into 1a (infrastructure) and 1b (skeleton app), each with its own acceptance check.
- Reason: milestone 1 was too large for one planning cycle.
- Approved by: Champ
