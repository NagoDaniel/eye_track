# Baseline before the accuracy tuning (2026-10-25)

What the pipeline did on the moving-dot recordings before any tuning, so that a change can be judged against it.

- `dot_eval.py`: scores every session of a study against the dot's path (Pipeline A and B): median distance (as measured and drift-corrected, in the pauses and the movements), gaze lag, share within 0.12 of the diagonal; slope and correlation of gaze against dot per axis; signed error at each of the 10 rest positions; an affine-fit-on-first-half check. Run inside the api container, the study id is the argument:

  ```
  docker compose exec -T api python - 7 < docs/evidence/m6-tuning-baseline/dot_eval.py
  ```

- `baseline_study7.txt`: study 7 ("H3", six sessions of one person each, second webcam, A and B; 0202 with glasses, 0101 without; the study archive is `data/exports/study-7.zip`).
- `baseline_study6.txt`: study 6 ("h2", four sessions, first webcam, A only; B on AA0101 only; AA and CC with glasses; archive `data/exports/study-h2.zip`).

The sessions are in the local database and under `data/studies/6` and `data/studies/7` (not in git). Read `docs/PIPELINE_OVERVIEW.md` first.
