"""Download a model listed in `shared/models.json`, verify size and sha256, write atomically.

Standard library only, so it runs in any image. Usage: download_model.py NAME DEST [MANIFEST]
Single source of truth for model URLs and hashes (PLAN §1); also read by the frontend build.
"""

import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

DEFAULT_MANIFEST = Path(__file__).resolve().parent / "models.json"
_CHUNK = 1 << 20


class ModelError(Exception):
    pass


def download(manifest: Path, name: str, dest: Path) -> None:
    models = json.loads(manifest.read_text())
    if name not in models:
        raise ModelError(f"unknown model '{name}' (known: {', '.join(sorted(models))})")
    entry = models[name]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(entry["url"], timeout=120) as response, tmp.open("wb") as out:
            while chunk := response.read(_CHUNK):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
        if size != entry["bytes"]:
            raise ModelError(f"{name}: size {size} != expected {entry['bytes']}")
        if digest.hexdigest() != entry["sha256"]:
            raise ModelError(
                f"{name}: sha256 mismatch: got {digest.hexdigest()}, expected {entry['sha256']}"
            )
        os.replace(tmp, dest)
    except OSError as exc:
        raise ModelError(f"{name}: download failed: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
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
