import gzip
import json

import pytest

from app.consent import UnknownConsentVersion, consent_text, consent_versions
from app.events import assemble_events, parse_events


def gz(*events: dict[str, object]) -> bytes:
    return gzip.compress("".join(json.dumps(e) + "\n" for e in events).encode())


def test_placeholder_consent_text_is_available() -> None:
    assert "webcam video is recorded" in consent_text("v1")
    assert "v1" in consent_versions()


@pytest.mark.parametrize("bad", ["", "nope", "../v1", "v1/../v1", ".hidden", "v 1"])
def test_unknown_or_unsafe_consent_version(bad: str) -> None:
    with pytest.raises(UnknownConsentVersion):
        consent_text(bad)


def test_assemble_concatenates_chunks_in_order() -> None:
    a = {"t": 1.0, "type": "session_start", "data": {}}
    b = {"t": 2.0, "type": "video_play", "data": {"video_id": "1"}}
    c = {"t": 3.0, "type": "video_ended", "data": {"video_id": "1"}}
    jsonl = assemble_events([gz(a, b), gz(c)])
    assert [json.loads(x) for x in jsonl.splitlines()] == [a, b, c]
    assert [e.type for e in parse_events(jsonl)] == ["session_start", "video_play", "video_ended"]


def test_assemble_adds_missing_final_newline() -> None:
    chunk = gzip.compress(b'{"t": 1, "type": "session_start", "data": {}}')
    jsonl = assemble_events([chunk, chunk])
    assert len(jsonl.splitlines()) == 2


def test_assemble_names_the_bad_chunk() -> None:
    with pytest.raises(ValueError, match="events chunk 1 is not valid gzip"):
        assemble_events([gz({"t": 1, "type": "session_start", "data": {}}), b"not gzip"])


def test_parse_reports_the_bad_line() -> None:
    good = json.dumps({"t": 1, "type": "session_start", "data": {}}).encode()
    with pytest.raises(ValueError, match="events line 2"):
        parse_events(good + b"\n" + b'{"t": 2, "type": "mystery", "data": {}}\n')
    with pytest.raises(ValueError, match="events line 1"):
        parse_events(b"not json\n")
