"""Paths under `DATA_ROOT` (PLAN §7) and atomic file writes.

Paths are built only from integers, a UUID and a stream name from a fixed list, so nothing a
client sends can escape `DATA_ROOT`. Files are written to a temporary name in the same directory
and renamed, so a reader never sees a partial file (PLAN §7).
"""

import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, get_args

from app.schemas import Stream

STREAMS: tuple[str, ...] = get_args(Stream)


def video_dir(root: Path, video_id: int) -> Path:
    return root / "videos" / str(video_id)


def video_path(root: Path, video_id: int) -> Path:
    return video_dir(root, video_id) / "video.mp4"


def video_aois_path(root: Path, video_id: int) -> Path:
    return video_dir(root, video_id) / "aois.json"


def session_dir(root: Path, study_id: int, session_id: uuid.UUID) -> Path:
    return root / "studies" / str(study_id) / "sessions" / str(session_id)


def raw_dir(root: Path, study_id: int, session_id: uuid.UUID) -> Path:
    return session_dir(root, study_id, session_id) / "raw"


def chunk_path(root: Path, study_id: int, session_id: uuid.UUID, stream: Stream, seq: int) -> Path:
    if stream not in STREAMS:
        raise ValueError(f"unknown stream {stream!r}")
    if seq < 0:
        raise ValueError(f"seq must not be negative, got {seq}")
    return raw_dir(root, study_id, session_id) / "chunks" / stream / f"{seq:06d}.bin"


def meta_path(root: Path, study_id: int, session_id: uuid.UUID) -> Path:
    return raw_dir(root, study_id, session_id) / "meta.json"


def events_path(root: Path, study_id: int, session_id: uuid.UUID) -> Path:
    return raw_dir(root, study_id, session_id) / "events.jsonl"


@contextmanager
def atomic_writer(path: Path) -> Generator[BinaryIO]:
    """Open a temporary file next to `path`; rename it over `path` when the block succeeds.

    If the block raises, the temporary file is removed and `path` is left as it was.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    with atomic_writer(path) as f:
        f.write(data)


def browser_features_path(root: Path, study_id: int, session_id: uuid.UUID) -> Path:
    return raw_dir(root, study_id, session_id) / "browser_features.jsonl"


def stimulus_frames_path(root: Path, study_id: int, session_id: uuid.UUID) -> Path:
    return raw_dir(root, study_id, session_id) / "stimulus_frames.jsonl"


def webcam_path(root: Path, study_id: int, session_id: uuid.UUID) -> Path:
    return raw_dir(root, study_id, session_id) / "webcam.webm"


def derived_dir(root: Path, study_id: int, session_id: uuid.UUID) -> Path:
    return session_dir(root, study_id, session_id) / "derived"


def analysis_run_dir(root: Path, study_id: int, video_id: int, run_id: str) -> Path:
    """Where one `analyze_video` run writes its outputs (PLAN §7, §9)."""
    return root / "analysis" / str(study_id) / str(video_id) / run_id
