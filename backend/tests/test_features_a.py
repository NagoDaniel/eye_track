"""Pipeline A frame features (PLAN §8.4) on synthetic geometry and on real MediaPipe frames."""

import json
import math
from pathlib import Path
from typing import Any

import pytest

from worker.pipeline.features_a import (
    LANDMARK_COUNT,
    LEFT_EYE,
    RIGHT_EYE,
    SUBSET_POSITION,
    EyeLandmarks,
    frame_features,
    head_pose,
)

SHARED = Path(__file__).resolve().parents[2] / "shared"
FIXTURE: dict[str, Any] = json.loads((SHARED / "fixtures" / "face_frames.json").read_text())
PROTOCOL: dict[str, Any] = json.loads((SHARED / "protocol.json").read_text())
IDENTITY = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
ASPECT = 16 / 9


def rotation(yaw: float, pitch: float, roll: float) -> list[list[float]]:
    """R = Rz(roll) Ry(yaw) Rx(pitch), angles in degrees."""
    y, p, r = (math.radians(a) for a in (yaw, pitch, roll))
    rx: list[list[float]] = [
        [1, 0, 0],
        [0, math.cos(p), -math.sin(p)],
        [0, math.sin(p), math.cos(p)],
    ]
    ry: list[list[float]] = [
        [math.cos(y), 0, math.sin(y)],
        [0, 1, 0],
        [-math.sin(y), 0, math.cos(y)],
    ]
    rz: list[list[float]] = [
        [math.cos(r), -math.sin(r), 0],
        [math.sin(r), math.cos(r), 0],
        [0, 0, 1],
    ]

    def mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    return mul(rz, mul(ry, rx))


def matrix_of(
    rot: list[list[float]], t: tuple[float, float, float] = (0, 0, 0), scale: float = 1.0
) -> list[float]:
    rows = [[*(scale * v for v in rot[i]), t[i]] for i in range(3)]
    return [v for row in rows for v in row] + [0.0, 0.0, 0.0, 1.0]


def place_eye(
    lm: list[list[float]],
    eye: EyeLandmarks,
    centre: tuple[float, float],
    width: float,
    angle_deg: float = 0.0,
    iris_offset: tuple[float, float] = (0.0, 0.0),
    open_ratio: float = 0.3,
) -> None:
    """Put one eye into `lm`; the geometry is given in pixel-proportional units `(x * ASPECT, y)`.

    `iris_offset` is in eye widths along the eye-local axes; `angle_deg` turns the whole eye.
    """
    a = math.radians(angle_deg)
    ex, ey = (math.cos(a), math.sin(a)), (-math.sin(a), math.cos(a))

    def put(mesh_index: int, u: float, v: float) -> None:
        px = centre[0] + width * (u * ex[0] + v * ey[0])
        py = centre[1] + width * (u * ex[1] + v * ey[1])
        lm[SUBSET_POSITION[mesh_index]] = [px / ASPECT, py, 0.0]

    put(eye.corner_a, -0.5, 0.0)
    put(eye.corner_b, 0.5, 0.0)
    put(eye.upper_lid, 0.0, -open_ratio / 2)
    put(eye.lower_lid, 0.0, open_ratio / 2)
    for k, mesh_index in enumerate(eye.iris):  # a ring of 4 around the offset centre, one in it
        dx, dy = ((0.0, 0.0), (0.05, 0.0), (-0.05, 0.0), (0.0, 0.05), (0.0, -0.05))[k]
        put(mesh_index, iris_offset[0] + dx, iris_offset[1] + dy)


def face(
    right: dict[str, Any] | None = None, left: dict[str, Any] | None = None
) -> list[list[float]]:
    lm = [[0.5, 0.5, 0.0] for _ in range(LANDMARK_COUNT)]
    place_eye(lm, RIGHT_EYE, (0.40 * ASPECT, 0.45), 0.08, **(right or {}))
    place_eye(lm, LEFT_EYE, (0.60 * ASPECT, 0.45), 0.08, **(left or {}))
    return lm


# --- eye-local iris position, openness, face scale ----------------------------------------------


def test_centred_iris_is_at_the_origin() -> None:
    f = frame_features(face(), IDENTITY, ASPECT)
    assert f.iris_l == pytest.approx((0, 0), abs=1e-9)
    assert f.iris_r == pytest.approx((0, 0), abs=1e-9)


def test_iris_offset_is_measured_in_eye_widths_along_the_eye_axes() -> None:
    f = frame_features(
        face({"iris_offset": (0.25, 0.10)}, {"iris_offset": (-0.2, -0.05)}), IDENTITY, ASPECT
    )
    assert f.iris_r == pytest.approx((0.25, 0.10), abs=1e-9)
    assert f.iris_l == pytest.approx((-0.2, -0.05), abs=1e-9)


@pytest.mark.parametrize("angle", [-40.0, 15.0, 90.0])
def test_a_tilted_eye_gives_the_same_local_coordinates(angle: float) -> None:
    f = frame_features(
        face({"angle_deg": angle, "iris_offset": (0.25, 0.10)}, {"angle_deg": angle}),
        IDENTITY,
        ASPECT,
    )
    assert f.iris_r == pytest.approx((0.25, 0.10), abs=1e-9)
    assert f.open_r == pytest.approx(0.3, abs=1e-9)


def test_the_eye_width_scales_the_result_not_the_position() -> None:
    lm = face()
    place_eye(lm, RIGHT_EYE, (0.40 * ASPECT, 0.45), 0.16, iris_offset=(0.1, 0.0))
    assert frame_features(lm, IDENTITY, ASPECT).iris_r == pytest.approx((0.1, 0.0), abs=1e-9)


def test_openness_is_lid_distance_over_eye_width() -> None:
    f = frame_features(face({"open_ratio": 0.35}, {"open_ratio": 0.0}), IDENTITY, ASPECT)
    assert f.open_r == pytest.approx(0.35, abs=1e-9)
    assert f.open_l == pytest.approx(0.0, abs=1e-9)


def test_face_ratio_is_the_eye_distance_over_the_frame_width() -> None:
    # eye centres 0.2 of the frame width apart, on the same row
    assert frame_features(face(), IDENTITY, ASPECT).face_ratio == pytest.approx(0.2, abs=1e-9)
    # and the vertical offset counts in pixel-proportional units
    lm = face()
    place_eye(lm, LEFT_EYE, (0.60 * ASPECT, 0.45 + 0.1), 0.08)
    expected = math.hypot(0.2 * ASPECT, 0.1) / ASPECT
    assert frame_features(lm, IDENTITY, ASPECT).face_ratio == pytest.approx(expected, abs=1e-9)


def test_aspect_ratio_matters() -> None:
    lm = face()
    a = frame_features(lm, IDENTITY, ASPECT).iris_r
    place_eye(lm, RIGHT_EYE, (0.40 * ASPECT, 0.45), 0.08, angle_deg=30, iris_offset=(0.2, 0.1))
    with_aspect = frame_features(lm, IDENTITY, ASPECT).iris_r
    without = frame_features(lm, IDENTITY, 1.0).iris_r  # wrong aspect: the eye looks squashed
    assert a == pytest.approx((0, 0), abs=1e-9)
    assert with_aspect == pytest.approx((0.2, 0.1), abs=1e-9)
    assert without != pytest.approx((0.2, 0.1), abs=1e-3)


# --- head pose ----------------------------------------------------------------------------------


def test_identity_matrix_is_a_frontal_head() -> None:
    assert head_pose(IDENTITY) == pytest.approx((0, 0, 0, 0, 0, 0), abs=1e-12)


@pytest.mark.parametrize(
    ("yaw", "pitch", "roll"),
    [(0, 0, 0), (25, 0, 0), (0, -15, 0), (0, 0, 30), (-20, 10, 5), (60, -30, 15), (-45, 20, -35)],
)
def test_angles_round_trip(yaw: float, pitch: float, roll: float) -> None:
    out = head_pose(matrix_of(rotation(yaw, pitch, roll), (1.5, -3.0, -44.0)))
    assert out == pytest.approx((yaw, pitch, roll, 1.5, -3.0, -44.0), abs=1e-9)


def test_a_scaled_matrix_gives_the_same_angles() -> None:
    out = head_pose(matrix_of(rotation(20, -10, 5), scale=1.7))
    assert out[:3] == pytest.approx((20, -10, 5), abs=1e-9)


def test_pitch_is_positive_when_the_head_tilts_down() -> None:
    # A head tilted down turns its nose (canonical +z) towards the camera's -y (down).
    nose = [row[2] for row in rotation(0, 20, 0)]
    assert nose[1] < 0
    assert head_pose(matrix_of(rotation(0, 20, 0)))[1] > 0


def test_yaw_is_positive_when_the_face_turns_to_the_image_right() -> None:
    nose = [row[2] for row in rotation(20, 0, 0)]
    assert nose[0] > 0
    assert head_pose(matrix_of(rotation(20, 0, 0)))[0] > 0


@pytest.mark.parametrize(
    "bad", [[1.0] * 15, [1.0] * 17, [0.0] * 16], ids=["short", "long", "degenerate"]
)
def test_bad_matrix_is_an_error(bad: list[float]) -> None:
    with pytest.raises(ValueError):
        head_pose(bad)


def test_bad_landmark_count_and_aspect_are_errors() -> None:
    with pytest.raises(ValueError, match="landmarks"):
        frame_features(face()[:-1], IDENTITY, ASPECT)
    with pytest.raises(ValueError, match="aspect"):
        frame_features(face(), IDENTITY, 0.0)


def test_coinciding_eye_corners_are_an_error() -> None:
    lm = face()
    lm[SUBSET_POSITION[RIGHT_EYE.corner_b]] = list(lm[SUBSET_POSITION[RIGHT_EYE.corner_a]])
    with pytest.raises(ValueError, match="corners"):
        frame_features(lm, IDENTITY, ASPECT)


# --- the landmark subset file -------------------------------------------------------------------


def test_subset_has_the_landmarks_the_features_need() -> None:
    assert PROTOCOL["landmark_count"] == LANDMARK_COUNT
    for eye in (RIGHT_EYE, LEFT_EYE):
        wanted = [eye.corner_a, eye.corner_b, eye.upper_lid, eye.lower_lid, *eye.iris]
        assert all(i in SUBSET_POSITION for i in wanted)


# --- real MediaPipe frames ----------------------------------------------------------------------

FRAMES: list[dict[str, Any]] = FIXTURE["frames"]


def test_fixture_is_real_mediapipe_output_of_the_clip() -> None:
    assert len(FRAMES) >= 40
    assert FIXTURE["clip"] == "e2e/fixtures/face.mp4"
    assert FIXTURE["aspect"] == pytest.approx(854 / 480, abs=1e-3)
    assert all(len(f["lm"]) == PROTOCOL["landmark_count"] for f in FRAMES)
    assert all(len(f["matrix"]) == PROTOCOL["feature_matrix_size"] for f in FRAMES)


def test_stored_features_are_what_the_code_computes_now() -> None:
    for frame in FRAMES:
        got = frame_features(frame["lm"], frame["matrix"], FIXTURE["aspect"]).as_dict()
        expected = frame["expected"]
        assert got.keys() == expected.keys()
        for key, value in expected.items():
            assert got[key] == pytest.approx(value, abs=1e-9), (frame["frame"], key)


def test_real_frames_look_like_a_person_facing_the_screen() -> None:
    for frame in FRAMES:
        f = frame_features(frame["lm"], frame["matrix"], FIXTURE["aspect"])
        assert abs(f.yaw) < 25 and abs(f.pitch) < 25 and abs(f.roll) < 25, frame["frame"]
        assert -60 < f.tz < -25  # in front of the camera, cm
        assert 0.04 < f.face_ratio < 0.12
        for u, v in (f.iris_l, f.iris_r):
            assert abs(u) < 0.5 and abs(v) < 0.5  # the iris is inside the eye
        assert 0.0 <= f.open_l < 0.6 and 0.0 <= f.open_r < 0.6


def test_real_blinks_have_low_openness_and_a_high_blink_score() -> None:
    blinking = [f for f in FRAMES if f["blink_l"] > 0.5 and f["blink_r"] > 0.5]
    open_eyes = [f for f in FRAMES if f["blink_l"] < 0.2 and f["blink_r"] < 0.2]
    assert blinking and open_eyes  # the clip has blinks (PLAN §8.4)

    def openness(frames: list[dict[str, Any]]) -> float:
        feats = [frame_features(f["lm"], f["matrix"], FIXTURE["aspect"]) for f in frames]
        return sum(f.open_l + f.open_r for f in feats) / (2 * len(feats))

    assert openness(blinking) < 0.5 * openness(open_eyes)
