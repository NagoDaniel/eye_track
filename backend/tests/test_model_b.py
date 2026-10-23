"""Pipeline B ONNX model (PLAN §8.5): real MobileOne weights and a real face from the e2e clip."""

import json
import math
import os
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
import pytest

from worker.pipeline import model_b
from worker.pipeline.model_b import GazeModelB, ModelBError, decode_logits, preprocess

REPO = Path(__file__).resolve().parents[2]
FACE_CLIP = REPO / "e2e" / "fixtures" / "face.mp4"
MANIFEST = json.loads((REPO / "shared" / "models.json").read_text())
SMALL = "l2cs_mobileone_s0"

# The face in the clip is frontal and stays put (854x480 frames): a fixed square around it.
FACE_CENTER = (428, 255)
FACE_SIDE = 300
FRAME_STEP = 45  # frames 0, 45, ... of the 360-frame clip
PLAUSIBLE_RAD = 0.6  # |yaw| and |pitch| for a frontal face looking at the screen

Image = npt.NDArray[np.uint8]


def _face_crops() -> list[Image]:
    capture = cv2.VideoCapture(str(FACE_CLIP))
    assert capture.isOpened()
    crops: list[Image] = []
    for index in range(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT)), FRAME_STEP):
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, bgr = capture.read()
        assert ok
        x0, y0 = FACE_CENTER[0] - FACE_SIDE // 2, FACE_CENTER[1] - FACE_SIDE // 2
        square = bgr[y0 : y0 + FACE_SIDE, x0 : x0 + FACE_SIDE]
        resized = cv2.resize(
            square, (model_b.INPUT_SIZE, model_b.INPUT_SIZE), interpolation=cv2.INTER_AREA
        )
        crops.append(np.ascontiguousarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)))
    capture.release()
    return crops


@pytest.fixture(scope="module")
def crops() -> list[Image]:
    return _face_crops()


@pytest.fixture(scope="module")
def model(model_b_root: Path) -> GazeModelB:
    return GazeModelB.load(model_b_root, SMALL)


def test_real_face_gaze_is_finite_and_plausible(model: GazeModelB, crops: list[Image]) -> None:
    pitch, yaw = model.predict(crops)
    print(f"pitch rad {np.round(pitch, 3).tolist()}\nyaw rad {np.round(yaw, 3).tolist()}")
    assert len(crops) == 8
    assert pitch.shape == yaw.shape == (len(crops),)
    assert pitch.dtype == yaw.dtype == np.float64
    assert np.isfinite(pitch).all() and np.isfinite(yaw).all()
    assert np.abs(pitch).max() < PLAUSIBLE_RAD
    assert np.abs(yaw).max() < PLAUSIBLE_RAD


def test_batch_equals_single_calls_and_batch_size_is_irrelevant(
    model: GazeModelB, crops: list[Image]
) -> None:
    pitch, yaw = model.predict(crops, batch_size=len(crops))
    singles = [model.predict([crop]) for crop in crops]
    np.testing.assert_allclose(pitch, [s[0][0] for s in singles], atol=1e-4)
    np.testing.assert_allclose(yaw, [s[1][0] for s in singles], atol=1e-4)
    for batch_size in (1, 3, 8, 100):
        p, y = model.predict(crops, batch_size=batch_size)
        np.testing.assert_allclose(p, pitch, atol=1e-4)
        np.testing.assert_allclose(y, yaw, atol=1e-4)


def test_deterministic(model: GazeModelB, crops: list[Image]) -> None:
    first = model.predict(crops[:3])
    second = model.predict(crops[:3])
    np.testing.assert_array_equal(first[0], second[0])
    np.testing.assert_array_equal(first[1], second[1])


def test_predict_accepts_an_array_batch(model: GazeModelB, crops: list[Image]) -> None:
    from_list = model.predict(crops[:2])
    from_array = model.predict(np.stack(crops[:2]))
    np.testing.assert_array_equal(from_list[0], from_array[0])


def test_predict_rejects_empty_input_and_bad_batch_size(
    model: GazeModelB, crops: list[Image]
) -> None:
    with pytest.raises(ModelBError, match="no images"):
        model.predict([])
    with pytest.raises(ModelBError, match="batch_size"):
        model.predict(crops[:1], batch_size=0)


def test_loaded_model_identity_and_session_io_names(model: GazeModelB) -> None:
    assert model.name == SMALL
    assert model.sha256 == MANIFEST[SMALL]["sha256"]
    assert [o.name for o in model._session.get_outputs()] == ["yaw", "pitch"]
    assert [i.name for i in model._session.get_inputs()] == ["input"]


def _one_hot(bin_index: int) -> npt.NDArray[np.float64]:
    logits = np.zeros((1, model_b.BIN_COUNT))
    logits[0, bin_index] = 100.0
    return logits


def test_decode_one_hot_is_the_bin_angle() -> None:
    uniform = np.zeros((1, model_b.BIN_COUNT))
    for i in (0, 1, 45, 89):
        pitch, yaw = decode_logits(_one_hot(i), uniform)
        assert yaw[0] == pytest.approx(math.radians(i * 4 - 180), abs=1e-12)
        # uniform logits: the mean bin, 44.5
        assert pitch[0] == pytest.approx(math.radians(44.5 * 4 - 180), abs=1e-12)
    pitch, yaw = decode_logits(_one_hot(10), _one_hot(70))
    assert yaw[0] == pytest.approx(math.radians(10 * 4 - 180), abs=1e-12)
    assert pitch[0] == pytest.approx(math.radians(70 * 4 - 180), abs=1e-12)


def test_decode_symmetric_two_bin_distribution_is_the_mean() -> None:
    logits = np.full((1, model_b.BIN_COUNT), -1e3)
    logits[0, 20] = logits[0, 60] = 0.0
    _, yaw = decode_logits(logits, logits)
    assert yaw[0] == pytest.approx(math.radians(40 * 4 - 180), abs=1e-12)
    logits[0, 60] = math.log(3.0)  # weights 1/4 and 3/4
    _, yaw = decode_logits(logits, logits)
    assert yaw[0] == pytest.approx(math.radians((0.25 * 20 + 0.75 * 60) * 4 - 180), abs=1e-12)


def test_decode_is_stable_with_large_logits_and_batched() -> None:
    logits = np.zeros((2, model_b.BIN_COUNT))
    logits[0, 5] = 1e5
    logits[1, 80] = 1e5
    logits[1, 78] = 1e5
    pitch, yaw = decode_logits(logits, logits)
    assert np.isfinite(pitch).all() and np.isfinite(yaw).all()
    assert yaw[0] == pytest.approx(math.radians(5 * 4 - 180), abs=1e-12)
    assert yaw[1] == pytest.approx(math.radians(79 * 4 - 180), abs=1e-12)
    np.testing.assert_array_equal(pitch, yaw)


def test_decode_rejects_wrong_shape() -> None:
    with pytest.raises(ModelBError, match="shape"):
        decode_logits(np.zeros((1, 10)), np.zeros((1, 10)))


def test_preprocess_normalises_a_constant_image() -> None:
    size = model_b.INPUT_SIZE
    image = np.empty((size, size, 3), dtype=np.uint8)
    image[...] = (255, 0, 51)  # R, G, B
    out = preprocess([image])
    assert out.shape == (1, 3, size, size) and out.dtype == np.float32
    expected = [
        (1.0 - 0.485) / 0.229,
        (0.0 - 0.456) / 0.224,
        (51 / 255 - 0.406) / 0.225,
    ]
    for channel in range(3):
        np.testing.assert_allclose(out[0, channel], expected[channel], atol=1e-6)


def test_preprocess_is_channel_first_rgb() -> None:
    size = model_b.INPUT_SIZE
    image = np.zeros((size, size, 3), dtype=np.uint8)
    image[3, 7] = (10, 20, 30)
    out = preprocess(image[None])
    assert out[0, 0, 3, 7] == pytest.approx((10 / 255 - 0.485) / 0.229, abs=1e-6)
    assert out[0, 1, 3, 7] == pytest.approx((20 / 255 - 0.456) / 0.224, abs=1e-6)
    assert out[0, 2, 3, 7] == pytest.approx((30 / 255 - 0.406) / 0.225, abs=1e-6)


def test_preprocess_rejects_wrong_size_dtype_and_empty() -> None:
    with pytest.raises(ModelBError, match="448"):
        preprocess([np.zeros((224, 224, 3), dtype=np.uint8)])
    with pytest.raises(ModelBError, match="448"):
        preprocess([np.zeros((448, 448, 3), dtype=np.float32)])
    with pytest.raises(ModelBError, match="no images"):
        preprocess([])


def test_missing_weights_say_how_to_get_them(tmp_path: Path) -> None:
    with pytest.raises(ModelBError, match=r"scripts/fetch_models\.sh") as info:
        GazeModelB.load(tmp_path, SMALL)
    assert str(tmp_path / MANIFEST[SMALL]["file"]) in str(info.value)


def test_wrong_sha256_names_expected_and_actual(model_b_root: Path, tmp_path: Path) -> None:
    data = bytearray((model_b_root / MANIFEST[SMALL]["file"]).read_bytes())
    data[len(data) // 2] ^= 0xFF
    corrupt = tmp_path / MANIFEST[SMALL]["file"]
    corrupt.write_bytes(data)
    with pytest.raises(ModelBError) as info:
        GazeModelB.load(tmp_path, SMALL)
    message = str(info.value)
    assert str(corrupt) in message
    assert MANIFEST[SMALL]["sha256"] in message
    assert model_b._sha256_of(corrupt) in message


def test_unknown_model_name_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ModelBError, match="unknown model"):
        GazeModelB.load(tmp_path, "nope")


def test_cuda_request_without_cuda_falls_back_with_a_warning(
    model_b_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    if "CUDAExecutionProvider" in ort.get_available_providers():
        pytest.skip("CUDA is available here")
    with caplog.at_level("WARNING", logger=model_b.log.name):
        loaded = GazeModelB.load(model_b_root, SMALL, provider="cuda")
    assert "CUDA" in caplog.text
    assert loaded._session.get_providers() == ["CPUExecutionProvider"]


@pytest.mark.skipif(
    not Path(os.environ.get("L2CS_RESNET50", "")).is_file(),
    reason="set L2CS_RESNET50 to the path of resnet50_gaze.onnx to run (95 MB, not downloaded)",
)
def test_resnet50_weights_on_a_real_face(crops: list[Image]) -> None:
    resnet = GazeModelB.load(Path(os.environ["L2CS_RESNET50"]).parent)
    pitch, yaw = resnet.predict(crops[:2])
    assert np.isfinite(pitch).all() and np.isfinite(yaw).all()
    assert np.abs(pitch).max() < PLAUSIBLE_RAD and np.abs(yaw).max() < PLAUSIBLE_RAD
