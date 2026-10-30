# Baseline before the accuracy tuning (2026-10-25, completed 2026-10-30)

What the pipeline did on the moving-dot recordings before any tuning, so that a change can be judged against it.

**Units** (found 2026-10-30): table 1 of every file comes from `moving_dot_report` and is in **viewport px** (CSS px of the participant's screen), not px of the 1280 x 720 video; tables 2 to 4 are in **video px**. The video filled 1.50 viewport px per video px in study 7 and 1.34 in study 6 (the `scale` column of the lab's files). Earlier docs called both "px of the video".

- `dot_eval.py`: the original scoring script (run inside the api container: `docker compose exec -T api python - 7 < docs/evidence/m6-tuning-baseline/dot_eval.py`). Its tables are now `backend/lab/score.py`; use the lab (`backend/lab/README.md`).
- `baseline_study7.txt`: study 7 ("H3", six sessions, second webcam, A and B; 0202 with glasses, 0101 without; archive `data/exports/study-7.zip`), made by `dot_eval.py`.
- `baseline_study6.txt`: study 6 ("h2", four sessions, first webcam, A only; B on AA0101 only; AA and CC with glasses; archive `data/exports/study-h2.zip`). The 2026-10-25 file was cut off after the first row of table 3; regenerated 2026-10-30 with `make lab ARGS="reference 6"` (same numbers in the rows it had).
- `baseline_lab.txt`: `make lab ARGS="run baseline"`: the pipeline re-run in the lab on both studies (2026-10-30, git `78d99b2` plus the lab). It reproduces the product's outputs in `data/` to floating-point noise (largest difference 1e-8 ms in time, 1e-11 in the model weights, all 10 sessions, A and B). Every experiment is compared with this run.

All sessions are of one person (the human).

The sessions are in the local database and under `data/studies/6` and `data/studies/7` (not in git). Read `docs/PIPELINE_OVERVIEW.md` first.
