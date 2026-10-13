"""The `events` stream on the server side (PLAN §5.9, §8.1).

Each chunk of the stream is a gzip-compressed block of JSON lines `{t, type, data}` (DECISIONS 1b
item 5). The server stores chunks untouched; these functions decode them.
"""

import gzip
import json
from collections.abc import Iterable

from app.schemas import EventRecord


def assemble_events(chunks: Iterable[bytes]) -> bytes:
    """Gunzip the chunks in order and concatenate them into one JSONL file (`raw/events.jsonl`).

    Raises ValueError naming the chunk position if one is not valid gzip.
    """
    out: list[bytes] = []
    for i, chunk in enumerate(chunks):
        try:
            text = gzip.decompress(chunk)
        except (OSError, EOFError) as exc:
            raise ValueError(f"events chunk {i} is not valid gzip: {exc}") from exc
        if text and not text.endswith(b"\n"):
            text += b"\n"
        out.append(text)
    return b"".join(out)


def parse_events(jsonl: bytes) -> list[EventRecord]:
    """Parse and validate every line. Raises ValueError (with the line number) on a bad line."""
    events: list[EventRecord] = []
    for n, line in enumerate(jsonl.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(EventRecord.model_validate(json.loads(line)))
        except ValueError as exc:  # json errors and pydantic ValidationError are ValueErrors
            raise ValueError(f"events line {n} is invalid: {exc}") from exc
    return events
