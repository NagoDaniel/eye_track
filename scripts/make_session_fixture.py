"""Turn a session exported by the e2e test into the pipeline tests' fixture (PLAN §13, §14 M3).

    E2E_EXPORT_SESSION=/tmp/session make e2e
    python3 scripts/make_session_fixture.py /tmp/session

The export holds the `raw/` directory of one complete session as the server wrote it (chunks, the
assembled JSONL files, `meta.json`) and `chunks.json`, the server's chunk table for that session
(stream, seq, size, sha256). The fixture is a real MediaRecorder recording of the human's face clip
(`e2e/fixtures/face.mp4`) played by Chromium's fake camera: its gaze does not follow the dots, so it
tests plumbing and the stages' maths on real data, not gaze accuracy.
"""

import json
import shutil
import sys
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent.parent / "shared" / "fixtures" / "session_e2e"
RAW_FILES = ("meta.json", "events.jsonl", "browser_features.jsonl", "stimulus_frames.jsonl")


def main(export: Path) -> None:
    manifest = json.loads((export / "chunks.json").read_text(encoding="utf-8"))
    if not manifest:
        raise SystemExit("chunks.json is empty")
    raw = FIXTURE / "raw"
    shutil.rmtree(FIXTURE, ignore_errors=True)
    raw.mkdir(parents=True)
    for name in RAW_FILES:
        shutil.copy2(export / name, raw / name)
    shutil.copytree(export / "chunks", raw / "chunks")
    rows = [
        {"stream": r["stream"], "seq": r["seq"], "size": r["size"], "sha256": r["sha256"]}
        for r in manifest
    ]
    (FIXTURE / "chunks.json").write_text(json.dumps(rows, indent=0) + "\n", encoding="utf-8")
    total = sum(f.stat().st_size for f in FIXTURE.rglob("*") if f.is_file())
    print(f"{len(rows)} chunks, {total / 1e6:.1f} MB written to {FIXTURE}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]))
