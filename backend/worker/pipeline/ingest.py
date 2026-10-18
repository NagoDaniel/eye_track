"""Ingest stage (PLAN §8.1): check the chunks, assemble the raw files, list the webcam frames.

Reads `raw/chunks/`, writes `raw/webcam.webm`, `raw/events.jsonl`, `raw/browser_features.jsonl`,
`raw/stimulus_frames.jsonl` (all deterministic assemblies of the immutable chunks, DECISIONS
milestone 3 item 4) and `derived/webcam_frames.parquet`. Idempotent: running it again gives the same
files. The sha256 of each chunk comes from the caller (the `chunks` table), not from the files, so a
chunk that changed on disk is found.
"""

import hashlib
import shutil
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import av
import pandas as pd
from pydantic import BaseModel

from app.events import assemble_events, parse_events, parse_records
from app.schemas import FeatureRecord, StimulusFrameRecord, Stream
from app.storage import (
    atomic_write_bytes,
    atomic_writer,
    browser_features_path,
    chunk_path,
    derived_dir,
    events_path,
    stimulus_frames_path,
    webcam_path,
)

WEBCAM_FRAMES_FILE = "webcam_frames.parquet"
INTERMEDIATES_DIR = "intermediates"
# Cues (the seek index) element id in Matroska/WebM; MediaRecorder files have none, remuxed ones do.
CUES_ID = b"\x1c\x53\xbb\x6b"
FFMPEG_ERROR_TAIL = 600


class IngestError(Exception):
    """The raw data of a session cannot be assembled; the message says what is wrong."""


@dataclass(frozen=True)
class ChunkRecord:
    """One row of the `chunks` table."""

    stream: Stream
    seq: int
    size: int
    sha256: str


@dataclass(frozen=True)
class IngestSummary:
    chunks: dict[str, int]  # per stream
    bytes: dict[str, int]  # per stream, as stored
    lines: dict[str, int]  # per JSONL stream
    webcam_frames: int
    webcam_duration_s: float  # last frame pts, in seconds
    webcam_fps_mean: float
    webcam_pts_strictly_increasing: bool
    webcam_codec: str
    webcam_width: int
    webcam_height: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


JSONL_STREAMS: tuple[Stream, ...] = ("events", "features", "stimulus_frames")


def _check_manifest(
    root: Path, study_id: int, session_id: uuid.UUID, manifest: Sequence[ChunkRecord]
) -> dict[Stream, list[ChunkRecord]]:
    """Group the chunks by stream in seq order and verify presence, size and sha256."""
    streams: dict[Stream, list[ChunkRecord]] = {}
    for record in sorted(manifest, key=lambda r: (r.stream, r.seq)):
        streams.setdefault(record.stream, []).append(record)
    problems: list[str] = []
    for stream, records in streams.items():
        seqs = [r.seq for r in records]
        gaps = sorted(set(range(seqs[-1] + 1)) - set(seqs))
        if gaps or len(set(seqs)) != len(seqs):
            problems.append(f"{stream}: chunks {gaps} are missing (or a seq is listed twice)")
        for record in records:
            path = chunk_path(root, study_id, session_id, stream, record.seq)
            try:
                data = path.read_bytes()
            except OSError as exc:
                problems.append(f"{stream} chunk {record.seq}: cannot read the file ({exc})")
                continue
            if len(data) != record.size:
                problems.append(
                    f"{stream} chunk {record.seq}: {len(data)} bytes on disk, {record.size} stored"
                )
            elif hashlib.sha256(data).hexdigest() != record.sha256:
                problems.append(f"{stream} chunk {record.seq}: sha256 does not match")
    for stream in ("webcam", "events"):
        if stream not in streams:
            problems.append(f"{stream}: there are no chunks")
    if problems:
        raise IngestError("the chunks are not complete and intact: " + "; ".join(problems))
    return streams


def _read(root: Path, study_id: int, session_id: uuid.UUID, stream: Stream, seq: int) -> bytes:
    return chunk_path(root, study_id, session_id, stream, seq).read_bytes()


def _write_assembled(path: Path, data: bytes) -> None:
    """Write a file that is a pure function of the chunks; one that differs is an error."""
    if path.exists():
        if path.read_bytes() != data:
            raise IngestError(f"{path.name} exists and differs from what the chunks give")
        return
    atomic_write_bytes(path, data)


def _assemble_jsonl(
    root: Path,
    study_id: int,
    session_id: uuid.UUID,
    stream: Stream,
    records: list[ChunkRecord],
    max_inflated_bytes: int,
    path: Path,
    model: type[BaseModel] | None,
) -> int:
    chunks = [_read(root, study_id, session_id, stream, r.seq) for r in records]
    try:
        jsonl = assemble_events(chunks, max_inflated_bytes=max_inflated_bytes, name=stream)
        lines = (
            len(parse_events(jsonl)) if model is None else len(parse_records(jsonl, model, stream))
        )
    except ValueError as exc:
        raise IngestError(str(exc)) from exc
    _write_assembled(path, jsonl)
    return lines


def _remux(
    root: Path,
    study_id: int,
    session_id: uuid.UUID,
    records: list[ChunkRecord],
    keep_concat: Path | None = None,
) -> Path:
    """Concatenate the webcam chunks and remux them so the file has a duration and cues.

    ffmpeg writes to a real file, not a pipe: only a seekable output gets the duration and the seek
    index. The result is renamed into place when ffmpeg succeeds.
    """
    out = webcam_path(root, study_id, session_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    concatenated = out.with_name(f".{out.name}.{token}.concat")
    remuxed = out.with_name(f".{out.name}.{token}.tmp")
    try:
        with concatenated.open("wb") as f:
            for record in records:
                f.write(_read(root, study_id, session_id, "webcam", record.seq))
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(concatenated), "-c", "copy",
             "-fflags", "+genpts", "-f", "webm", str(remuxed)],
            capture_output=True,
            check=False,
        )  # fmt: skip
        if result.returncode != 0 or not remuxed.exists() or remuxed.stat().st_size == 0:
            tail = result.stderr.decode(errors="replace")[-FFMPEG_ERROR_TAIL:]
            raise IngestError(f"ffmpeg could not remux the webcam recording: {tail.strip()}")
        remuxed.replace(out)
    finally:
        if keep_concat is not None and concatenated.exists():
            keep_concat.parent.mkdir(parents=True, exist_ok=True)
            concatenated.replace(keep_concat)  # for debugging: also when ffmpeg failed
        concatenated.unlink(missing_ok=True)
        remuxed.unlink(missing_ok=True)
    return out


def _webcam_frames(path: Path) -> tuple[pd.DataFrame, str, int, int]:
    """Presentation time of every video packet (one per shown frame) from the remuxed file."""
    try:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            time_base = stream.time_base
            if time_base is None:
                raise IngestError("the webcam recording has no time base")
            pts = [
                float(packet.pts * time_base)
                for packet in container.demux(stream)
                if packet.pts is not None
            ]
            codec = stream.codec_context.name
            width, height = stream.codec_context.width, stream.codec_context.height
    except av.error.FFmpegError as exc:
        raise IngestError(f"the webcam recording cannot be read: {exc}") from exc
    if not pts:
        raise IngestError("the webcam recording has no video frames")
    return pd.DataFrame({"index": range(len(pts)), "pts": pts}), codec, width, height


def ingest(
    root: Path,
    study_id: int,
    session_id: uuid.UUID,
    manifest: Sequence[ChunkRecord],
    max_inflated_bytes: int,
    keep_intermediates: bool = False,
) -> IngestSummary:
    """Run the stage. `IngestError` means the raw data is incomplete, corrupt or unreadable.

    With `keep_intermediates` the concatenated webcam chunks (before the remux) are kept in
    `derived/intermediates/`; otherwise that directory is removed."""
    streams = _check_manifest(root, study_id, session_id, manifest)
    paths = {
        "events": events_path(root, study_id, session_id),
        "features": browser_features_path(root, study_id, session_id),
        "stimulus_frames": stimulus_frames_path(root, study_id, session_id),
    }
    models: dict[str, type[BaseModel] | None] = {
        "events": None,
        "features": FeatureRecord,
        "stimulus_frames": StimulusFrameRecord,
    }
    lines: dict[str, int] = {
        stream: _assemble_jsonl(
            root, study_id, session_id, stream, streams[stream], max_inflated_bytes,
            paths[stream], models[stream],
        )
        for stream in JSONL_STREAMS
        if stream in streams
    }  # fmt: skip

    intermediates = derived_dir(root, study_id, session_id) / INTERMEDIATES_DIR
    if not keep_intermediates:
        shutil.rmtree(intermediates, ignore_errors=True)
    webcam = _remux(
        root, study_id, session_id, streams["webcam"],
        intermediates / "webcam_concat.webm" if keep_intermediates else None,
    )  # fmt: skip
    frames, codec, width, height = _webcam_frames(webcam)
    out = derived_dir(root, study_id, session_id) / WEBCAM_FRAMES_FILE
    with atomic_writer(out) as f:
        frames.to_parquet(f, compression="snappy", index=False)

    pts = frames["pts"]
    duration = float(pts.iloc[-1])
    return IngestSummary(
        chunks={s: len(r) for s, r in streams.items()},
        bytes={s: sum(x.size for x in r) for s, r in streams.items()},
        lines=lines,
        webcam_frames=len(frames),
        webcam_duration_s=duration,
        webcam_fps_mean=(len(frames) - 1) / duration if duration > 0 else 0.0,
        webcam_pts_strictly_increasing=bool((pts.diff().dropna() > 0).all()),
        webcam_codec=codec,
        webcam_width=width,
        webcam_height=height,
    )
