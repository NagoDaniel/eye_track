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
