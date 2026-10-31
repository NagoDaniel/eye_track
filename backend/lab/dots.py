"""The moving-dot videos the lab can score against, recognised by the sha256 of the video file.

A study may play several dot videos (the long recording of T13 plays `long_dot_1` and
`long_dot_2`); `lab import-study` hashes each played video in the source data root and records in
the lab session which video id is which dot video (`dot_videos.json`). Videos that are not dot
videos are not scored.
"""

import hashlib
import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from app.events import parse_events
from app.storage import events_path, video_path
from lab.layout import REPO_ROOT
from lab.score import DotTruth

LAB_VIDEOS = Path(__file__).resolve().parent / "videos"
# name -> (video file, ground-truth path file)
KNOWN: dict[str, tuple[Path, Path]] = {
    "moving_dot": (
        REPO_ROOT / "e2e" / "fixtures" / "moving_dot.mp4",
        REPO_ROOT / "e2e" / "fixtures" / "moving_dot_path.json",
    ),
    "long_dot_1": (LAB_VIDEOS / "long_dot_1.mp4", LAB_VIDEOS / "long_dot_1_path.json"),
    "long_dot_2": (LAB_VIDEOS / "long_dot_2.mp4", LAB_VIDEOS / "long_dot_2_path.json"),
}
DOT_VIDEOS_FILE = "dot_videos.json"  # in a lab session: {video id: dot video name}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@cache
def _by_hash() -> dict[str, str]:
    return {sha256(video): name for name, (video, _) in KNOWN.items()}


def identify(video: Path) -> str | None:
    """The dot video's name, or None when the file is not a known dot video."""
    return _by_hash().get(sha256(video))


@cache
def truth(name: str) -> DotTruth:
    return DotTruth.load(KNOWN[name][1])


def played_dot_videos(source: Path, study_id: int, session_id: object) -> dict[str, str]:
    """{video id: dot video name} for the dot videos a session played, from its events and the
    video files of the source data root."""
    import uuid

    sid = session_id if isinstance(session_id, uuid.UUID) else uuid.UUID(str(session_id))
    events = parse_events(events_path(source, study_id, sid).read_bytes())
    out: dict[str, str] = {}
    for e in events:
        if e.type != "video_load":
            continue
        vid = str(e.data["video_id"])
        file = video_path(source, int(vid))
        if vid not in out and file.is_file() and (name := identify(file)) is not None:
            out[vid] = name
    return out


@dataclass(frozen=True)
class DotVideo:
    video_id: str
    name: str


def read_dot_videos(session_dir: Path) -> list[DotVideo]:
    path = session_dir / DOT_VIDEOS_FILE
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} is missing: import the study again (`lab import-study <id> --force`)"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return [DotVideo(vid, name) for vid, name in data.items()]
