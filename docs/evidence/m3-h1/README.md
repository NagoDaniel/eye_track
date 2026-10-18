# Milestone 3, H1 acceptance evidence (PLAN §12.4, §14)

Aggregate numbers only: no gaze rows, no video, no landmarks. Both sessions are real recordings of the
moving-dot video made by the project owner (debug sessions, `is_debug` true).

| | Session A | Session B |
|---|---|---|
| Id | `080ebab8-8576-429b-b7ec-f11fc26ba2f7` | `fb226e78-8b9e-4236-b23b-e323a3073e64` |
| Conditions | glasses, 3 calibration attempts | no glasses |
| Valid frames | 643 of 654 (98.3 %) | see file, 98 % |
| Median error | 197 px = 0.098 of the diagonal | 92 px = 0.046 |
| Gaze lag | about +160 ms (gaze behind the dot) | about +160 ms |
| Sync | 3 windows, lag about -33 ms, peak 0.997, no flags | 1 window, lag -53.5 ms, peak 0.999, flag `sync_short` |

Criterion: median error at most 0.15 of the screen diagonal (`dot_max_median_error_norm`, DECISIONS
2026-10-16 item 9) and at least 0.80 valid frames. That criterion is looser than the quality gate
(0.10) and the inclusion limit (0.12); both sessions pass it, A with little margin to the gate.
Precision tuning is deferred (DECISIONS 2026-10-18, "pilot result").

Files:
- `session-a-moving-dot.json`, `session-b-moving-dot.json`: output of `moving-dot-report <id> --json`.
- `session-a-sync.json`, `session-b-sync.json`: the window list, lag, peak and flags of `derived/sync.json`.

Reproduce (read-only):

    docker compose -f docker-compose.yml -f compose.dev.yml exec -T api python cli.py moving-dot-report <id> --json
