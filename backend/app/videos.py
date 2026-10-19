"""Stimulus video administration (PLAN §4, §6, §7): upload, probe, metadata, delete.

Plain functions used by the JSON and form routes. Uploads are copied to disk in blocks while the
sha256 is computed, then probed with `ffprobe`; a file that is not a video leaves nothing behind.
"""

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.aois import parse_aoi_file
from app.models import Study, Video
from app.service_errors import ConflictError, InvalidError, NotFoundError, TooLargeError
from app.storage import atomic_write_bytes, atomic_writer, video_aois_path, video_dir, video_path

MIB = 1024 * 1024
UPLOAD_BLOCK_BYTES = MIB
# Multipart framing and the other form fields, on top of the file, in `Content-Length`.
MULTIPART_OVERHEAD_BYTES = MIB
FFPROBE_TIMEOUT_S = 120
# Relative difference between `r_frame_rate` and `avg_frame_rate` above which a video is called
# variable frame rate (constant frame rate files differ only by rounding).
VFR_RELATIVE_TOLERANCE = 0.01
EXPECTED_CODEC = "h264"
EXPECTED_CONTAINER = "mp4"


@dataclass(frozen=True)
class Probe:
    duration: float
    width: int
    height: int
    fps: float
    warnings: list[str]


def _rate(value: object) -> Fraction:
    """Parse ffprobe's `25/1`; `0/0` (unknown) gives 0."""
    try:
        num, _, den = str(value).partition("/")
        return Fraction(int(num), int(den or 1))
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def probe_video(path: Path) -> Probe:
    """Run ffprobe. Raises InvalidError when the file has no video stream."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
             str(path)],
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_S,
        )  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        raise InvalidError(f"ffprobe did not finish within {FFPROBE_TIMEOUT_S} s") from exc
    if result.returncode != 0:
        raise InvalidError(
            f"not a readable video file: {result.stderr.strip() or 'ffprobe failed'}"
        )
    info: dict[str, Any] = json.loads(result.stdout)
    streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        raise InvalidError("the file has no video stream")
    stream = streams[0]
    fmt: dict[str, Any] = info.get("format", {})

    r_rate, avg_rate = _rate(stream.get("r_frame_rate")), _rate(stream.get("avg_frame_rate"))
    fps = avg_rate or r_rate
    duration = float(stream.get("duration") or fmt.get("duration") or 0)
    if fps <= 0 or duration <= 0:
        raise InvalidError("cannot determine the frame rate and duration of the video")

    warnings: list[str] = []
    codec = str(stream.get("codec_name"))
    if codec != EXPECTED_CODEC:
        warnings.append(f"video codec is {codec}, not H.264: it may not play in every browser")
    if r_rate > 0 and avg_rate > 0 and abs(r_rate - avg_rate) / r_rate > VFR_RELATIVE_TOLERANCE:
        warnings.append(
            f"variable frame rate (r_frame_rate {r_rate}, avg_frame_rate {avg_rate}): "
            "gaze timing against video frames will be less exact"
        )
    if EXPECTED_CONTAINER not in str(fmt.get("format_name", "")).split(","):
        warnings.append(f"container is {fmt.get('format_name')}, not MP4")
    return Probe(
        duration=duration,
        width=int(stream["width"]),
        height=int(stream["height"]),
        fps=float(fps),
        warnings=warnings,
    )


def _clean_title(title: str) -> str:
    title = title.strip()
    if not title:
        raise InvalidError("title must not be empty")
    return title


def create_video(
    db: DbSession, data_root: Path, title: str, source: BinaryIO, max_bytes: int
) -> tuple[Video, list[str]]:
    """Store an upload and commit its row. On any failure the DB row and the files are removed."""
    video = Video(
        title=_clean_title(title), file_path="", duration=0, width=0, height=0, fps=0, sha256=""
    )
    db.add(video)
    db.flush()
    video_id = video.id  # rollback expires the row; keep the id for the cleanup
    try:
        digest, total = hashlib.sha256(), 0
        with atomic_writer(video_path(data_root, video_id)) as out:
            while block := source.read(UPLOAD_BLOCK_BYTES):
                total += len(block)
                if total > max_bytes:
                    raise TooLargeError(f"upload is larger than {max_bytes // MIB} MiB")
                digest.update(block)
                out.write(block)
        probe = probe_video(video_path(data_root, video_id))
        video.file_path = f"videos/{video_id}/video.mp4"
        video.duration, video.width, video.height = probe.duration, probe.width, probe.height
        video.fps, video.sha256 = probe.fps, digest.hexdigest()
        db.commit()
    except BaseException:
        db.rollback()
        shutil.rmtree(video_dir(data_root, video_id), ignore_errors=True)
        raise
    return video, probe.warnings


def get_video(db: DbSession, video_id: int) -> Video:
    video = db.get(Video, video_id)
    if video is None:
        raise NotFoundError(f"video {video_id} not found")
    return video


def list_videos(db: DbSession) -> list[Video]:
    return list(db.scalars(select(Video).order_by(Video.id)))


def parse_aois(raw: str) -> dict[str, Any] | None:
    """Form input for the AOIs: empty means none, otherwise a JSON object."""
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise InvalidError(f"aois is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise InvalidError("aois must be a JSON object")
    parse_aoi_file(value)
    return value


def update_video(
    db: DbSession, data_root: Path, video_id: int, title: str, aois: dict[str, Any] | None
) -> Video:
    """Set title and AOIs; `aois.json` mirrors the database value (PLAN §7)."""
    video = get_video(db, video_id)
    title = _clean_title(title)
    if aois is not None:
        parse_aoi_file(aois)
    video.title = title
    video.aois = aois
    path = video_aois_path(data_root, video_id)
    if aois is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_bytes(path, json.dumps(aois, indent=2).encode())
    db.commit()
    return video


def find_references(db: DbSession, data_root: Path, video_id: int) -> list[str]:
    """Where a video is still used: study playlists and session config snapshots."""
    wanted = str(video_id)
    found = [
        f"study {s.id} ({s.code!r}) playlist"
        for s in db.scalars(select(Study).order_by(Study.id))
        if wanted in s.config.get("playlist", [])
    ]
    for meta in sorted(data_root.glob("studies/*/sessions/*/raw/meta.json")):
        where = str(meta.relative_to(data_root))
        try:
            playlist = json.loads(meta.read_text())["config"]["playlist"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Cannot tell whether this session uses the video: refuse rather than guess.
            raise ConflictError(f"cannot read {where} to check video use: {exc}") from exc
        if wanted in playlist:
            found.append(where)
    return found


def delete_video(db: DbSession, data_root: Path, video_id: int) -> None:
    video = get_video(db, video_id)
    used = find_references(db, data_root, video_id)
    if used:
        raise ConflictError(f"video {video_id} is still used by: {', '.join(used)}")
    db.delete(video)
    db.commit()
    shutil.rmtree(video_dir(data_root, video_id), ignore_errors=True)


def video_out(video: Video, data_root: Path) -> dict[str, Any]:
    file = data_root / video.file_path
    return {
        "id": video.id,
        "title": video.title,
        "file_path": video.file_path,
        "duration": video.duration,
        "width": video.width,
        "height": video.height,
        "fps": video.fps,
        "sha256": video.sha256,
        "size_bytes": file.stat().st_size if file.is_file() else None,
        "aois": video.aois,
    }
