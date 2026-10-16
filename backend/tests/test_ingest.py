"""Ingest stage (PLAN §8.1) on the real recorded session in `shared/fixtures/session_e2e/`.

Real ffmpeg and PyAV; nothing is mocked.
"""

import dataclasses
import gzip
import hashlib
import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from tests.session_fixture import FixtureSession, copy_fixture_session
from worker.pipeline.ingest import CUES_ID, ChunkRecord, IngestError, ingest

MAX_INFLATED = 32 * 1024 * 1024


@pytest.fixture
def session(tmp_path: Path) -> FixtureSession:
    return copy_fixture_session(tmp_path)


def run(s: FixtureSession, manifest: list[ChunkRecord] | None = None):  # type: ignore[no-untyped-def]
    return ingest(s.root, s.study_id, s.session_id, manifest or s.manifest, MAX_INFLATED)


def probe(path: Path) -> dict[str, object]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json",
         "-show_entries", "format=duration:stream=codec_name,width,height", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    return json.loads(out)  # type: ignore[no-any-return]


def chunk_file(s: FixtureSession, stream: str, seq: int) -> Path:
    return s.raw / "chunks" / stream / f"{seq:06d}.bin"


def replace_chunk(s: FixtureSession, stream: str, seq: int, data: bytes) -> list[ChunkRecord]:
    """New bytes for one chunk, with the manifest updated so only the content is wrong."""
    chunk_file(s, stream, seq).write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    return [
        dataclasses.replace(r, size=len(data), sha256=digest)
        if (r.stream, r.seq) == (stream, seq)
        else r
        for r in s.manifest
    ]


def test_ingest_assembles_the_session(session: FixtureSession) -> None:
    committed = {
        name: (session.raw / name).read_bytes()
        for name in ("events.jsonl", "browser_features.jsonl", "stimulus_frames.jsonl")
    }
    for name in committed:
        (session.raw / name).unlink()

    summary = run(session)

    # The JSONL files are exactly what the server wrote at /complete.
    for name, content in committed.items():
        assert (session.raw / name).read_bytes() == content
    assert summary.lines == {
        "events": len(committed["events.jsonl"].splitlines()),
        "features": len(committed["browser_features.jsonl"].splitlines()),
        "stimulus_frames": len(committed["stimulus_frames.jsonl"].splitlines()),
    }
    assert summary.chunks["webcam"] == 6 and summary.chunks["events"] == 8
    assert summary.bytes["webcam"] == sum(r.size for r in session.manifest if r.stream == "webcam")

    # The remuxed recording is a seekable WebM with a duration and the seek index.
    webm = session.raw / "webcam.webm"
    info = probe(webm)
    stream = info["streams"][0]  # type: ignore[index]
    assert stream["codec_name"] in ("vp8", "vp9") and stream["width"] >= 640
    assert float(info["format"]["duration"]) > 5  # type: ignore[index]
    assert CUES_ID in webm.read_bytes()

    # One pts per frame, increasing, and close to the length of the face clip played in the browser.
    frames = pd.read_parquet(
        session.root / "studies" / str(session.study_id) / "sessions" / str(session.session_id)
        / "derived" / "webcam_frames.parquet"
    )  # fmt: skip
    assert list(frames.columns) == ["index", "pts"]
    assert len(frames) == summary.webcam_frames > 100
    assert frames["pts"].is_monotonic_increasing and summary.webcam_pts_strictly_increasing
    assert frames["pts"].iloc[0] == pytest.approx(0, abs=0.1)
    assert summary.webcam_duration_s == pytest.approx(float(frames["pts"].iloc[-1]))
    assert 5 < summary.webcam_fps_mean < 40
    assert (summary.webcam_width, summary.webcam_height) == (stream["width"], stream["height"])


def test_the_frames_are_the_frames_ffprobe_sees(session: FixtureSession) -> None:
    summary = run(session)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0",
         str(session.raw / "webcam.webm")],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    assert summary.webcam_frames == int(out.strip())


def test_ingest_twice_gives_the_same_result(session: FixtureSession) -> None:
    first = run(session)
    second = run(session)
    assert first == second
    assert (session.raw / "webcam.webm").exists()
    assert [p.name for p in session.raw.iterdir() if p.name.startswith(".")] == []


def test_a_missing_chunk_file_fails_loudly(session: FixtureSession) -> None:
    chunk_file(session, "webcam", 2).unlink()
    with pytest.raises(IngestError, match=r"webcam chunk 2: cannot read"):
        run(session)


def test_a_chunk_missing_from_the_table_fails_loudly(session: FixtureSession) -> None:
    manifest = [r for r in session.manifest if (r.stream, r.seq) != ("features", 1)]
    with pytest.raises(IngestError, match=r"features: chunks \[1\] are missing"):
        run(session, manifest)


def test_a_chunk_changed_on_disk_fails_loudly(session: FixtureSession) -> None:
    path = chunk_file(session, "webcam", 1)
    data = bytearray(path.read_bytes())
    data[100] ^= 0xFF  # same size, other content
    path.write_bytes(bytes(data))
    with pytest.raises(IngestError, match=r"webcam chunk 1: sha256 does not match"):
        run(session)


def test_a_session_without_webcam_chunks_fails_loudly(session: FixtureSession) -> None:
    manifest = [r for r in session.manifest if r.stream != "webcam"]
    with pytest.raises(IngestError, match=r"webcam: there are no chunks"):
        run(session, manifest)


def test_a_corrupt_gzip_chunk_fails_loudly(session: FixtureSession) -> None:
    manifest = replace_chunk(session, "features", 0, b"this is not gzip")
    with pytest.raises(IngestError, match=r"features chunk 0 is not valid gzip"):
        run(session, manifest)


def test_an_invalid_record_fails_loudly(session: FixtureSession) -> None:
    manifest = replace_chunk(session, "stimulus_frames", 0, gzip.compress(b'{"t": 1}\n'))
    with pytest.raises(IngestError, match=r"stimulus_frames line 1 is invalid"):
        run(session, manifest)


def test_webcam_bytes_that_are_not_a_video_fail_loudly(session: FixtureSession) -> None:
    manifest = session.manifest
    for r in session.manifest:
        if r.stream == "webcam":
            manifest = replace_chunk(session, "webcam", r.seq, b"\x00" * 64)
            session = dataclasses.replace(session, manifest=manifest)
    with pytest.raises(IngestError, match=r"ffmpeg could not remux"):
        run(session)
    assert not (session.raw / "webcam.webm").exists()


def test_an_assembled_file_that_differs_is_not_overwritten(session: FixtureSession) -> None:
    (session.raw / "events.jsonl").write_bytes(b'{"t": 0, "type": "window_blur", "data": {}}\n')
    with pytest.raises(IngestError, match=r"events.jsonl exists and differs"):
        run(session)
