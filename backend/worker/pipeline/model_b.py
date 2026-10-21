"""Pipeline B gaze network: the L2CS-style ONNX model (PLAN §8.5, §11.2).

Weights: `shared/models.json` (`l2cs_resnet50` primary, `l2cs_mobileone_s0` small), yakhyo/gaze-
estimation ONNX release, fetched by `scripts/fetch_models.sh`. This module only runs the network on
faces that are already cropped; the crop rule is the caller's.

I/O of the ONNX file (checked with onnxruntime, both weights): input `input`, float32
[N, 3, 448, 448], RGB, pixels scaled to 0-1 and normalised with the ImageNet mean and std. Outputs
`yaw` and `pitch`, each [N, 90] logits over 90 bins of 4 degrees, looked up by name (never by
position). Decode: softmax over the bins, expected bin index, `angle_deg = expected * 4 - 180`.
Angles are returned as the network gives them (no sign flip): the caller maps them to the screen.
"""

import hashlib
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import numpy as np
import numpy.typing as npt
import onnxruntime as ort

log = logging.getLogger(__name__)

# `shared/` sits next to `backend/` in the repository and at `/shared` in the image.
_MANIFEST_FILE = Path(__file__).resolve().parents[3] / "shared" / "models.json"

INPUT_SIZE = 448  # the network's input side in pixels
BIN_COUNT = 90  # classes per angle
BIN_WIDTH_DEG = 4.0
BIN_OFFSET_DEG = -180.0  # angle at the (continuous) bin position 0
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
DEFAULT_MODEL = "l2cs_resnet50"
DEFAULT_BATCH_SIZE = 8  # not in the plan; ResNet-50 at 448x448 is heavy on a CPU
FETCH_HINT = "scripts/fetch_models.sh"

Provider = Literal["cpu", "cuda"]
Float64Array = npt.NDArray[np.float64]
Float32Array = npt.NDArray[np.float32]
Images = Sequence[npt.NDArray[np.uint8]] | npt.NDArray[np.uint8]


class ModelBError(Exception):
    """The Pipeline B model cannot be loaded or used (missing or corrupt weights, bad input)."""


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_entry(name: str, manifest: Path = _MANIFEST_FILE) -> dict[str, str]:
    models = json.loads(manifest.read_text(encoding="utf-8"))
    if name not in models:
        known = ", ".join(sorted(models))
        raise ModelBError(f"unknown model '{name}' in {manifest} (known: {known})")
    entry: dict[str, str] = models[name]
    return entry


def _decode(logits: npt.ArrayLike) -> Float64Array:
    x = np.asarray(logits, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] != BIN_COUNT:
        raise ModelBError(f"logits must have shape [N, {BIN_COUNT}], got {list(x.shape)}")
    p = np.exp(x - x.max(axis=1, keepdims=True))  # shifted by the maximum: stable for large logits
    p /= p.sum(axis=1, keepdims=True)
    expected = p @ np.arange(BIN_COUNT, dtype=np.float64)
    result: Float64Array = np.deg2rad(expected * BIN_WIDTH_DEG + BIN_OFFSET_DEG)
    return result


def decode_logits(
    yaw_logits: npt.ArrayLike, pitch_logits: npt.ArrayLike
) -> tuple[Float64Array, Float64Array]:
    """Logits [N, 90] of yaw and pitch to `(pitch_rad, yaw_rad)`, each [N] float64."""
    return _decode(pitch_logits), _decode(yaw_logits)


def preprocess(images: Images) -> Float32Array:
    """RGB uint8 images of 448x448 to the normalised float32 batch [N, 3, 448, 448]."""
    if len(images) == 0:
        raise ModelBError("no images to preprocess")
    batch = np.stack([np.asarray(image) for image in images])
    expected = (INPUT_SIZE, INPUT_SIZE, 3)
    if batch.dtype != np.uint8 or batch.shape[1:] != expected:
        raise ModelBError(
            f"images must be uint8 RGB of shape {list(expected)}, "
            f"got {batch.dtype} {list(batch.shape[1:])}"
        )
    mean = np.asarray(IMAGENET_MEAN, dtype=np.float32)
    std = np.asarray(IMAGENET_STD, dtype=np.float32)
    scaled = (batch.astype(np.float32) / np.float32(255.0) - mean) / std
    result: Float32Array = np.ascontiguousarray(scaled.transpose(0, 3, 1, 2), dtype=np.float32)
    return result


class GazeModelB:
    """The ONNX gaze network in an onnxruntime session."""

    def __init__(
        self, session: ort.InferenceSession, name: str, sha256: str, provider: Provider = "cpu"
    ) -> None:
        self._session = session
        self.name = name
        self.provider = provider  # the provider in use (a CUDA request falls back to the CPU)
        self.sha256 = sha256  # of the weights file, for the provenance of a run
        # The MobileOne export has a fixed batch dimension of 1; a dynamic one is a string or None.
        fixed = session.get_inputs()[0].shape[0]
        self.fixed_batch: int | None = fixed if isinstance(fixed, int) else None

    @classmethod
    def load(
        cls,
        models_root: Path,
        name: str = DEFAULT_MODEL,
        provider: Provider = "cpu",
        manifest: Path = _MANIFEST_FILE,
    ) -> "GazeModelB":
        entry = manifest_entry(name, manifest)
        path = models_root / entry["file"]
        if not path.is_file():
            raise ModelBError(
                f"Pipeline B weights '{name}' are missing: {path} does not exist. "
                f"Download them with {FETCH_HINT} {name}"
            )
        expected, actual = entry["sha256"], _sha256_of(path)
        if actual != expected:
            raise ModelBError(
                f"Pipeline B weights '{name}' at {path} are corrupt: "
                f"sha256 expected {expected}, actual {actual}. Re-download with {FETCH_HINT} {name}"
            )
        providers = ["CPUExecutionProvider"]
        used: Provider = "cpu"
        if provider == "cuda":
            if "CUDAExecutionProvider" in ort.get_available_providers():
                providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
                used = "cuda"
            else:
                log.warning("CUDA requested for model B but not available; using the CPU")
        session = ort.InferenceSession(str(path), providers=providers)
        outputs = [o.name for o in session.get_outputs()]
        if not {"yaw", "pitch"} <= set(outputs):
            raise ModelBError(f"{path}: expected outputs 'yaw' and 'pitch', found {outputs}")
        return cls(session, name, actual, used)

    def predict(
        self, images: Images, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> tuple[Float64Array, Float64Array]:
        """Gaze of each face crop as `(pitch_rad, yaw_rad)`, each [N] float64.

        Runs `batch_size` images per call, or one per call when the ONNX file has a fixed batch
        dimension of 1 (the case for the MobileOne weights), so the result never depends on it.
        """
        if batch_size < 1:
            raise ModelBError(f"batch_size must be at least 1, got {batch_size}")
        if len(images) == 0:
            raise ModelBError("no images to predict")
        if self.fixed_batch is not None:
            batch_size = self.fixed_batch
        pitches: list[Float64Array] = []
        yaws: list[Float64Array] = []
        for start in range(0, len(images), batch_size):
            batch = preprocess(images[start : start + batch_size])
            yaw_logits, pitch_logits = self._session.run(["yaw", "pitch"], {"input": batch})
            pitch, yaw = decode_logits(yaw_logits, pitch_logits)
            pitches.append(pitch)
            yaws.append(yaw)
        return np.concatenate(pitches), np.concatenate(yaws)
