"""Download a model listed in `shared/models.json`, verify size and sha256, write atomically.

Standard library only, so it runs in any image. Usage:
  download_model.py NAME DEST_FILE [MANIFEST]            download one model to an exact path
  download_model.py fetch [--manifest M] DIR NAME...     download models into DIR under their
                                                         manifest file names; skips files that
                                                         already match (used by fetch_models.sh)
Single source of truth for model URLs and hashes (PLAN §1); also read by the frontend build.
"""

import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_MANIFEST = Path(__file__).resolve().parent / "models.json"
_CHUNK = 1 << 20
DOWNLOAD_ATTEMPTS = 3  # network stalls are common on slow links; integrity errors are not retried
TIMEOUT_S = 60
RETRY_DELAY_S = 5


class ModelError(Exception):
    pass


def _download_once(url: str, tmp: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response, tmp.open("wb") as out:
        while chunk := response.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    return size, digest.hexdigest()


def download(manifest: Path, name: str, dest: Path) -> None:
    models = json.loads(manifest.read_text())
    if name not in models:
        raise ModelError(f"unknown model '{name}' (known: {', '.join(sorted(models))})")
    entry = models[name]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                size, sha256 = _download_once(entry["url"], tmp)
                break
            except OSError as exc:
                if attempt == DOWNLOAD_ATTEMPTS:
                    raise ModelError(
                        f"{name}: download failed after {attempt} attempts: {exc}"
                    ) from exc
                print(f"warning: {name}: {exc}; retrying ({attempt}/{DOWNLOAD_ATTEMPTS})",
                      file=sys.stderr)  # fmt: skip
                time.sleep(RETRY_DELAY_S)
        if size != entry["bytes"]:
            raise ModelError(f"{name}: size {size} != expected {entry['bytes']}")
        if sha256 != entry["sha256"]:
            raise ModelError(f"{name}: sha256 mismatch: got {sha256}, expected {entry['sha256']}")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(manifest: Path, names: list[str], dest_dir: Path) -> dict[str, str]:
    """Download `names` into `dest_dir` as `<manifest file name>`. Returns name -> status.

    Validates every name and file name first, so nothing is downloaded for a bad request. An
    existing file with the right sha256 is kept; a corrupted one is replaced; a failed download
    never touches an existing file.
    """
    models = json.loads(manifest.read_text())
    for name in names:
        if name not in models:
            raise ModelError(f"unknown model '{name}' (known: {', '.join(sorted(models))})")
        file_name = models[name].get("file", "")
        if not file_name or Path(file_name).name != file_name or file_name in (".", ".."):
            raise ModelError(f"{name}: invalid file name '{file_name}' in manifest")
    result: dict[str, str] = {}
    for name in names:
        dest = dest_dir / models[name]["file"]
        if dest.is_file() and _sha256_of(dest) == models[name]["sha256"]:
            result[name] = "already present"
            continue
        download(manifest, name, dest)
        result[name] = "downloaded"
    return result


def _main_fetch(args: list[str]) -> int:
    manifest = DEFAULT_MANIFEST
    if args[:1] == ["--manifest"] and len(args) >= 2:
        manifest, args = Path(args[1]), args[2:]
    if len(args) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        result = fetch(manifest, args[1:], Path(args[0]))
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for name, status in result.items():
        print(f"ok: {name}: {status}")
    return 0


def main(argv: list[str]) -> int:
    if argv[1:2] == ["fetch"]:
        return _main_fetch(argv[2:])
    if len(argv) not in (3, 4):
        print(__doc__, file=sys.stderr)
        return 2
    manifest = Path(argv[3]) if len(argv) == 4 else DEFAULT_MANIFEST
    try:
        download(manifest, argv[1], Path(argv[2]))
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"ok: {argv[1]} -> {argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
