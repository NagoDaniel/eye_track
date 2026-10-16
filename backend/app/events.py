"""The JSONL streams on the server side (PLAN §5.4, §5.9, §8.1).

Each chunk of the `events`, `features` and `stimulus_frames` streams is a gzip-compressed block of
JSON lines (DECISIONS 1b item 5). The server stores chunks untouched; these functions decode them.
"""

import json
import zlib
from collections.abc import Iterable
from typing import TypeVar

from pydantic import BaseModel

from app.schemas import CalibGateResultData, EventRecord

M = TypeVar("M", bound=BaseModel)


def gunzip_limited(chunk: bytes, max_bytes: int) -> bytes:
    """Decompress one gzip stream, never producing more than `max_bytes`.

    A few kilobytes of gzip can inflate to gigabytes, so the output is bounded while it is made.
    Raises ValueError for corrupt, truncated or oversized data and for bytes after the stream.
    """
    inflater = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)  # 16: gzip container
    try:
        out = inflater.decompress(chunk, max_bytes + 1)
        if len(out) > max_bytes:
            raise ValueError(f"inflates to more than {max_bytes} bytes")
        if not inflater.eof:
            raise ValueError("the gzip stream is truncated")
    except zlib.error as exc:  # not an OSError or ValueError, so it needs its own clause
        raise ValueError(str(exc)) from exc
    if inflater.unused_data:
        raise ValueError("unexpected bytes after the gzip stream")
    return out


def assemble_events(
    chunks: Iterable[bytes], *, max_inflated_bytes: int, name: str = "events"
) -> bytes:
    """Gunzip the chunks in order and concatenate them into one JSONL file (`raw/events.jsonl`).

    Each chunk may inflate to at most `max_inflated_bytes`. Raises ValueError naming the stream
    (`name`) and the chunk position if one is not valid gzip or too large.
    """
    out: list[bytes] = []
    for i, chunk in enumerate(chunks):
        try:
            text = gunzip_limited(chunk, max_inflated_bytes)
        except ValueError as exc:
            raise ValueError(f"{name} chunk {i} is not valid gzip: {exc}") from exc
        if text and not text.endswith(b"\n"):
            text += b"\n"
        out.append(text)
    return b"".join(out)


def parse_records(jsonl: bytes, model: type[M], name: str) -> list[M]:
    """Parse and validate every line as `model`. Raises ValueError naming `name` and the line."""
    records: list[M] = []
    for n, line in enumerate(jsonl.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate(json.loads(line)))
        except ValueError as exc:  # json errors and pydantic ValidationError are ValueErrors
            raise ValueError(f"{name} line {n} is invalid: {exc}") from exc
    return records


def parse_events(jsonl: bytes) -> list[EventRecord]:
    """Parse and validate every line. Raises ValueError (with the line number) on a bad line."""
    return parse_records(jsonl, EventRecord, "events")


def calibration_gate_failed(events: Iterable[EventRecord]) -> bool:
    """True when the last pre-video gate result is a failure (PLAN §5.6, DECISIONS milestone 2).

    A skipped gate (`passed` null) and results of later validation blocks do not count. Raises
    ValueError if a `calib_gate_result` event has data of the wrong shape.
    """
    failed = False
    for event in events:
        if event.type != "calib_gate_result":
            continue
        try:
            result = CalibGateResultData.model_validate(event.data)
        except ValueError as exc:
            raise ValueError(f"calib_gate_result at t={event.t} is invalid: {exc}") from exc
        if result.block == "before_video" and not result.skipped:
            failed = result.passed is False
    return failed
