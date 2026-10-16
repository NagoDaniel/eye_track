"""Regenerate `shared/fixtures/face_frames.json` from the face clip (PLAN §5.6 step 2, §13).

Runs the real MediaPipe Face Landmarker (Python) over `e2e/fixtures/face.mp4`, keeps the landmark
subset, the transformation matrix and the blink scores of every STEP-th frame, and stores them next
to the features that `backend/worker/pipeline/features_a.py` computes from them. The TypeScript
test compares its own result with those numbers, so both implementations are tested on real
MediaPipe output.

Run from `backend/`:  uv run python ../scripts/make_face_fixture.py
"""

import json
import sys
from pathlib import Path

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import FaceLandmarker, FaceLandmarkerOptions, RunningMode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from worker.pipeline.features_a import SUBSET_POSITION, frame_features  # noqa: E402

CLIP = ROOT / "e2e" / "fixtures" / "face.mp4"
MODEL = ROOT / "frontend" / "public" / "mediapipe" / "face_landmarker.task"
OUT = ROOT / "shared" / "fixtures" / "face_frames.json"
STEP = 8  # about 45 frames of a 12 s clip at 30 fps
DECIMALS = 6


def main() -> None:
    options = FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(MODEL)),
        running_mode=RunningMode.VIDEO,
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=True,
    )
    mesh_indices = sorted(SUBSET_POSITION, key=SUBSET_POSITION.__getitem__)
    capture = cv2.VideoCapture(str(CLIP))
    fps = capture.get(cv2.CAP_PROP_FPS)
    width = capture.get(cv2.CAP_PROP_FRAME_WIDTH)
    height = capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
    aspect = width / height
    frames: list[dict[str, object]] = []
    total = with_face = 0
    with FaceLandmarker.create_from_options(options) as landmarker:
        index = 0
        while True:
            ok, bgr = capture.read()
            if not ok:
                break
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            result = landmarker.detect_for_video(image, round(index / fps * 1000))
            total += 1
            if result.face_landmarks:
                with_face += 1
                if index % STEP == 0:
                    mesh = result.face_landmarks[0]
                    scores = {b.category_name: b.score for b in result.face_blendshapes[0]}
                    frames.append(
                        {
                            "frame": index,
                            "lm": [
                                [round(v, DECIMALS) for v in (mesh[i].x, mesh[i].y, mesh[i].z)]
                                for i in mesh_indices
                            ],
                            "matrix": [
                                round(float(v), DECIMALS)
                                for v in result.facial_transformation_matrixes[0].flatten()
                            ],
                            "blink_l": round(scores["eyeBlinkLeft"], DECIMALS),
                            "blink_r": round(scores["eyeBlinkRight"], DECIMALS),
                        }
                    )
            index += 1
    for frame in frames:
        features = frame_features(frame["lm"], frame["matrix"], aspect)  # type: ignore[arg-type]
        frame["expected"] = features.as_dict()
    document = {
        "comment": "Real MediaPipe output; regenerate with scripts/make_face_fixture.py",
        "clip": "e2e/fixtures/face.mp4",
        "aspect": aspect,
        "frames": frames,
    }
    OUT.write_text(json.dumps(document, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{with_face}/{total} frames with a face; {len(frames)} frames kept; {OUT.stat().st_size} bytes")


if __name__ == "__main__":
    main()
