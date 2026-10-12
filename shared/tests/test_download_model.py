import hashlib
import json
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import download_model  # noqa: E402
from download_model import ModelError, download, fetch  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download_model, "RETRY_DELAY_S", 0)


def manifest_for(
    tmp_path: Path, payload: bytes, sha256: str | None = None, file: str = "m.onnx"
) -> Path:
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
                    "file": file,
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
        assert entry["file"] and "/" not in entry["file"], name


def test_fetch_downloads_into_dir_using_manifest_file_name(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    result = fetch(manifest_for(tmp_path, b"weights"), ["m"], dest)
    assert result == {"m": "downloaded"}
    assert (dest / "m.onnx").read_bytes() == b"weights"


def test_fetch_skips_file_that_already_matches(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    manifest = manifest_for(tmp_path, b"weights")
    fetch(manifest, ["m"], dest)
    (tmp_path / "src.bin").unlink()  # a second download would now fail
    assert fetch(manifest, ["m"], dest) == {"m": "already present"}


def test_fetch_replaces_a_corrupted_file(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    manifest = manifest_for(tmp_path, b"weights")
    dest.mkdir()
    (dest / "m.onnx").write_bytes(b"corrupt")
    assert fetch(manifest, ["m"], dest) == {"m": "downloaded"}
    assert (dest / "m.onnx").read_bytes() == b"weights"


def test_fetch_wrong_hash_keeps_existing_file_and_fails(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    dest.mkdir()
    (dest / "m.onnx").write_bytes(b"old")
    with pytest.raises(ModelError, match="sha256 mismatch"):
        fetch(manifest_for(tmp_path, b"weights", sha256="0" * 64), ["m"], dest)
    assert (dest / "m.onnx").read_bytes() == b"old"


def test_fetch_unknown_name_fails_before_downloading_anything(tmp_path: Path) -> None:
    dest = tmp_path / "models"
    with pytest.raises(ModelError, match="unknown model 'nope'"):
        fetch(manifest_for(tmp_path, b"x"), ["m", "nope"], dest)
    assert not (dest / "m.onnx").exists()


def test_fetch_rejects_file_names_that_escape_the_directory(tmp_path: Path) -> None:
    with pytest.raises(ModelError, match="invalid file name"):
        fetch(manifest_for(tmp_path, b"x", file="../evil.onnx"), ["m"], tmp_path / "models")


class _Flaky:
    """urlopen stand-in: fails `failures` times with a timeout, then really opens the URL."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self._real = urllib.request.urlopen

    def __call__(self, url: str, timeout: float) -> object:
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("The read operation timed out")
        return self._real(url, timeout=timeout)


def test_transient_timeouts_are_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flaky = _Flaky(failures=2)
    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    dest = tmp_path / "m.bin"
    download(manifest_for(tmp_path, b"weights"), "m", dest)
    assert dest.read_bytes() == b"weights"
    assert flaky.calls == 3


def test_gives_up_after_the_configured_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = _Flaky(failures=99)
    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    with pytest.raises(ModelError, match="download failed"):
        download(manifest_for(tmp_path, b"weights"), "m", tmp_path / "m.bin")
    assert flaky.calls == download_model.DOWNLOAD_ATTEMPTS


def test_hash_mismatch_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flaky = _Flaky(failures=0)
    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    with pytest.raises(ModelError, match="sha256 mismatch"):
        download(manifest_for(tmp_path, b"weights", sha256="0" * 64), "m", tmp_path / "m.bin")
    assert flaky.calls == 1
