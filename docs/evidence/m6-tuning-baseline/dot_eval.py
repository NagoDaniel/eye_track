"""Accuracy of Pipeline A and B against the moving dot, for every session of a study.

Run inside the api container (it reads /data and /shared and imports the pipeline code):

    docker compose exec -T api python - 7 < docs/evidence/m6-tuning-baseline/dot_eval.py

The argument is the study id. The study must have played the moving-dot video (21.8 s, 1280 x 720,
ground truth in /shared/moving_dot_path.json) and its sessions must be processed (B: process-b).
Everything is in pixels of the 1280 x 720 video unless it says "norm" (video widths / heights).

Tables printed:
 1. per session and pipeline: valid share, median distance to the dot as measured ("unc") and
    drift-corrected ("dc"), in the pauses and in the movements, the gaze lag, share of frames within
    0.12 of the viewport diagonal;
 2. per session and pipeline: gaze = a * dot + b per axis (a < 1: pulled towards the centre) and the
    correlation;
 3. per rest position of the dot, over all sessions: mean signed error (gaze - dot) with its spread;
 4. an offline "what a gain/offset fit would give" check: fit gaze -> dot on the first half of the
    video, score on the second half (a plain affine correction does NOT help: the error is not a
    constant gain or offset).
"""

import glob
import json
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from app.events import parse_events
from worker.pipeline.config import PipelineConfig
from worker.pipeline.moving_dot import DotPath, moving_dot_report

STUDY = int(sys.argv[1]) if len(sys.argv) > 1 else 7
W, H = 1280, 720
raw_path = json.load(open("/shared/moving_dot_path.json"))
dot_path = DotPath.load(Path("/shared/moving_dot_path.json"))
sessions = []
for d in sorted(glob.glob(f"/data/studies/{STUDY}/sessions/*")):
    meta = json.load(open(d + "/raw/meta.json"))
    sessions.append((meta["participant_code"], d))


def col(t, name):
    return np.array([np.nan if v is None else v for v in t[name].to_pylist()])


def frames(t, x_name="x_vid", y_name="y_vid"):
    """Valid playback frames: stimulus time, gaze (px), dot (px)."""
    phase = np.array(t["phase"].to_pylist(), dtype=object)
    valid = np.array(t["valid"].to_pylist(), dtype=bool)
    st = col(t, "stim_time")
    m = (phase == "playback") & valid & np.isfinite(st) & (st >= 0) & (st <= dot_path.duration_s)
    x, y, s = col(t, x_name)[m] * W, col(t, y_name)[m] * H, st[m]
    ok = np.isfinite(x) & np.isfinite(y)
    x, y, s = x[ok], y[ok], s[ok]
    tx, ty = dot_path.at(s)
    return s, x, y, tx * W, ty * H


print("1. distance to the dot")
print(f"{'code':8}{'pl':3}{'valid':>6}{'unc med':>8}{'unc pause':>10}{'unc move':>9}{'dc med':>7}"
      f"{'dc pause':>9}{'dc move':>8}{'dc p90':>7}{'lag ms':>7}{'<=0.12':>7}")
for code, d in sessions:
    events = parse_events(open(d + "/raw/events.jsonl", "rb").read())
    for p in "ab":
        f = f"{d}/derived/gaze_{p}.parquet"
        if not Path(f).exists():
            continue
        r = moving_dot_report(pq.read_table(f), events, dot_path, PipelineConfig())
        u, c = r["uncorrected"], r["drift_corrected"]
        print(f"{code:8}{p:3}{r['valid_fraction']:6.2f}{u['median_px']:8.0f}{u['pauses']['median_px']:10.0f}"
              f"{u['movements']['median_px']:9.0f}{c['median_px']:7.0f}{c['pauses']['median_px']:9.0f}"
              f"{c['movements']['median_px']:8.0f}{c['p90_px']:7.0f}{r['gaze_lag_ms']:7.0f}"
              f"{u['within_threshold'] * 100:7.0f}")

print("\n2. gaze = a * dot + b (per axis), correlation r")
for code, d in sessions:
    for p in "ab":
        f = f"{d}/derived/gaze_{p}.parquet"
        if not Path(f).exists():
            continue
        s, x, y, tx, ty = frames(pq.read_table(f))
        ax, bx = np.polyfit(tx, x, 1)
        ay, by = np.polyfit(ty, y, 1)
        print(f"{code} {p}  x: a={ax:.2f} b={bx:+5.0f}px r={np.corrcoef(tx, x)[0, 1]:.2f}   "
              f"y: a={ay:.2f} b={by:+5.0f}px r={np.corrcoef(ty, y)[0, 1]:.2f}")

print("\n3. signed error (gaze - dot) at each rest position, mean +- spread over sessions (px)")
rows: dict[tuple[str, int], list[tuple[float, float]]] = {}
for code, d in sessions:
    for p in "ab":
        f = f"{d}/derived/gaze_{p}.parquet"
        if not Path(f).exists():
            continue
        s, x, y, _, _ = frames(pq.read_table(f))
        for i, q in enumerate(raw_path["pauses"]):
            sel = (s >= q["start_s"] + 0.5) & (s <= q["end_s"])
            if sel.sum() > 3:
                rows.setdefault((p, i), []).append((np.median(x[sel]) - q["x"], np.median(y[sel]) - q["y"]))
for i, q in enumerate(raw_path["pauses"]):
    cells = []
    for p in "ab":
        a = np.array(rows.get((p, i), [(np.nan, np.nan)]))
        cells.append(f"{p.upper()} dx {a[:, 0].mean():+5.0f}+-{a[:, 0].std():3.0f} dy {a[:, 1].mean():+5.0f}+-{a[:, 1].std():3.0f}")
    print(f"{i:2} ({q['x']:5.0f},{q['y']:4.0f}) | " + " | ".join(cells))

print("\n4. affine fit on the first half of the video (s < 11), median error on the second half")
for code, d in sessions:
    for p in "ab":
        f = f"{d}/derived/gaze_{p}.parquet"
        if not Path(f).exists():
            continue
        s, x, y, tx, ty = frames(pq.read_table(f))
        h1, h2 = s < 11, s >= 11
        px, py = np.polyfit(x[h1], tx[h1], 1), np.polyfit(y[h1], ty[h1], 1)
        before = np.median(np.hypot(x - tx, y - ty)[h2])
        after = np.median(np.hypot(np.polyval(px, x) - tx, np.polyval(py, y) - ty)[h2])
        print(f"{code} {p}  second half: {before:4.0f} px as measured, {after:4.0f} px after the affine fit")
