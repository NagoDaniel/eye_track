"""Storage paths and atomic writes on a real temp directory (PLAN §7, §1)."""

import threading
import uuid
from pathlib import Path
from typing import cast

import pytest

from app.schemas import Stream
from app.storage import (
    atomic_write_bytes,
    atomic_writer,
    chunk_path,
    events_path,
    meta_path,
    session_dir,
    video_aois_path,
    video_path,
)

SESSION = uuid.UUID("3f2b8c1e-5d4a-4e9b-9c1a-2f6d7e8a9b0c")


def test_paths_follow_plan_section_7(tmp_path: Path) -> None:
    sdir = tmp_path / "studies" / "4" / "sessions" / str(SESSION)
    assert video_path(tmp_path, 7) == tmp_path / "videos" / "7" / "video.mp4"
    assert video_aois_path(tmp_path, 7) == tmp_path / "videos" / "7" / "aois.json"
    assert session_dir(tmp_path, 4, SESSION) == sdir
    assert chunk_path(tmp_path, 4, SESSION, "webcam", 12) == sdir / "raw/chunks/webcam/000012.bin"
    assert meta_path(tmp_path, 4, SESSION) == sdir / "raw/meta.json"
    assert events_path(tmp_path, 4, SESSION) == sdir / "raw/events.jsonl"


@pytest.mark.parametrize("stream", ["../x", "video", "", "events/.."])
def test_unknown_stream_is_an_error(tmp_path: Path, stream: str) -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        chunk_path(tmp_path, 1, SESSION, cast(Stream, stream), 0)


def test_negative_seq_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="seq"):
        chunk_path(tmp_path, 1, SESSION, "events", -1)


def test_write_creates_parents_and_leaves_no_temp_file(tmp_path: Path) -> None:
    target = chunk_path(tmp_path, 1, SESSION, "events", 0)
    atomic_write_bytes(target, b"hello")
    assert target.read_bytes() == b"hello"
    assert [p.name for p in target.parent.iterdir()] == ["000000.bin"]


def test_failed_write_leaves_no_partial_file(tmp_path: Path) -> None:
    target = tmp_path / "out" / "video.mp4"
    with pytest.raises(RuntimeError, match="disk went away"), atomic_writer(target) as f:
        f.write(b"half a file")
        raise RuntimeError("disk went away")
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_failed_overwrite_keeps_the_old_content(tmp_path: Path) -> None:
    target = tmp_path / "meta.json"
    atomic_write_bytes(target, b"old")
    with pytest.raises(RuntimeError), atomic_writer(target) as f:
        f.write(b"new but broken")
        raise RuntimeError("boom")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["meta.json"]


def test_streamed_write(tmp_path: Path) -> None:
    target = tmp_path / "v" / "video.mp4"
    with atomic_writer(target) as f:
        for part in (b"aa", b"bb", b"cc"):
            f.write(part)
    assert target.read_bytes() == b"aabbcc"


def test_concurrent_writers_never_produce_a_mixed_file(tmp_path: Path) -> None:
    target = tmp_path / "meta.json"
    payloads = [bytes([65 + i]) * 200_000 for i in range(8)]
    threads = [threading.Thread(target=atomic_write_bytes, args=(target, p)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert target.read_bytes() in payloads
    assert [p.name for p in tmp_path.iterdir()] == ["meta.json"]
