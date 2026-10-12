import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from download_model import ModelError, download  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def manifest_for(tmp_path: Path, payload: bytes, sha256: str | None = None) -> Path:
    src = tmp_path / "src.bin"
    src.write_bytes(payload)
    manifest = tmp_path / "models.json"
    manifest.write_text(
        json.dumps(
            {
                "m": {
                    "url": src.as_uri(),
                    "sha256": sha256 or hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                }
            }
        )
    )
    return manifest


def test_download_writes_verified_file(tmp_path: Path) -> None:
    dest = tmp_path / "out" / "m.bin"
    download(manifest_for(tmp_path, b"weights"), "m", dest)
    assert dest.read_bytes() == b"weights"
    assert not list(dest.parent.glob("*.tmp"))


def test_wrong_hash_fails_and_leaves_nothing(tmp_path: Path) -> None:
    dest = tmp_path / "m.bin"
    with pytest.raises(ModelError, match="sha256 mismatch"):
        download(manifest_for(tmp_path, b"weights", sha256="0" * 64), "m", dest)
    assert not dest.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_wrong_hash_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    dest = tmp_path / "m.bin"
    dest.write_bytes(b"old")
    with pytest.raises(ModelError):
        download(manifest_for(tmp_path, b"weights", sha256="0" * 64), "m", dest)
    assert dest.read_bytes() == b"old"


def test_size_mismatch_fails(tmp_path: Path) -> None:
    payload = b"weights"
    manifest = manifest_for(tmp_path, payload)
    data = json.loads(manifest.read_text())
    data["m"]["bytes"] = 999
    manifest.write_text(json.dumps(data))
    with pytest.raises(ModelError, match="size"):
        download(manifest, "m", tmp_path / "m.bin")


def test_unknown_model_name_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(ModelError, match="unknown model 'nope'"):
        download(manifest_for(tmp_path, b"x"), "nope", tmp_path / "m.bin")


def test_unreachable_url_fails_loudly(tmp_path: Path) -> None:
    manifest = manifest_for(tmp_path, b"x")
    data = json.loads(manifest.read_text())
    data["m"]["url"] = (tmp_path / "missing.bin").as_uri()
    manifest.write_text(json.dumps(data))
    with pytest.raises(ModelError, match="download failed"):
        download(manifest, "m", tmp_path / "m.bin")


def test_real_manifest_is_well_formed() -> None:
    models = json.loads((REPO / "shared" / "models.json").read_text())
    assert {"face_landmarker", "l2cs_resnet50", "l2cs_mobileone_s0"} <= set(models)
    for name, entry in models.items():
        assert entry["url"].startswith("https://"), name
        assert len(entry["sha256"]) == 64 and int(entry["sha256"], 16) >= 0, name
        assert entry["bytes"] > 0, name
