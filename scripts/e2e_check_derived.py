"""Check the derived files of one processed session (PLAN §13: all files exist, schemas valid).

Run inside the api container by scripts/e2e.sh:  python - <derived dir> < this file
Prints one summary line; exits non-zero with a message on the first problem.
"""

import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

derived = Path(sys.argv[1])
raw = derived.parent / "raw"


def fail(message: str) -> None:
    raise SystemExit(f"{derived.parent.name[:8]}: {message}")


def columns(name: str) -> list[str]:
    path = derived / name
    if not path.is_file():
        fail(f"{name} is missing")
    return list(pq.read_schema(path).names)


def need(name: str, expected: list[str], prefix_only: bool = False) -> None:
    got = columns(name)
    if (got[: len(expected)] if prefix_only else got) != expected:
        fail(f"{name} has columns {got}, expected {expected}")


if not (raw / "webcam.webm").is_file():
    fail("raw/webcam.webm is missing")
need("webcam_frames.parquet", ["index", "pts"])
need("landmarks.parquet", ["index", "pts", "face", "lm", "matrix", "blink_l", "blink_r"])
features = columns("features_a.parquet")
for column in ("index", "pts", "face", "iris_l_x", "yaw", "face_ratio", "blink"):
    if column not in features:
        fail(f"features_a.parquet has no column {column}")
frames = pq.read_metadata(derived / "webcam_frames.parquet").num_rows
for name in ("landmarks.parquet", "features_a.parquet"):
    if pq.read_metadata(derived / name).num_rows != frames:
        fail(f"{name} does not have one row per webcam frame")

sync = json.loads((derived / "sync.json").read_text())
for key in ("t_of_pts", "lag_ms", "peak", "windows", "curve", "flags"):
    if key not in sync:
        fail(f"sync.json has no {key}")
calibration = json.loads((derived / "calibration_a.json").read_text())
if calibration["status"] not in ("ok", "failed"):
    fail("calibration_a.json has no status")

quality = json.loads((derived / "quality.json").read_text())
for key in ("webcam", "events", "calibration", "sync", "inclusion", "stages"):
    if key not in quality:
        fail(f"quality.json has no {key}")
stages = list(quality["stages"])
if stages != ["ingest", "landmarks", "features", "sync", "calibration", "gaze", "quality", "plots"]:
    fail(f"quality.json stages are {stages}")
plots = quality["stages"]["plots"]["summary"]
if plots["errors"]:
    fail(f"plots failed: {plots['errors']}")
for name in plots["written"]:
    data = (derived / "debug" / name).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 5000:
        fail(f"debug/{name} is not a real PNG")
for name in ("calibration.png", "sync.png", "signals.png"):
    if name not in plots["written"]:
        fail(f"debug/{name} was not drawn: {plots['skipped'].get(name)}")

if calibration["status"] == "ok":
    need("gaze_a.parquet", [
        "t", "webcam_pts", "video_id", "stim_time", "x_px", "y_px", "x_sm", "y_sm", "x_vid", "y_vid",
        "valid", "blink", "conf", "phase",
    ], prefix_only=True)  # fmt: skip
    if pq.read_metadata(derived / "gaze_a.parquet").num_rows != frames:
        fail("gaze_a.parquet does not have one row per webcam frame")
    need("fixations_a.parquet", ["id", "start_t", "end_t", "duration_ms"], prefix_only=True)
elif (derived / "gaze_a.parquet").exists():
    fail("a failed calibration left a gaze file")

print(
    f"{derived.parent.name[:8]}: {frames} frames, calibration {calibration['status']}, "
    f"sync flags {sync['flags']}, included {quality['inclusion']['included']} "
    f"{quality['inclusion']['reasons']}"
)
