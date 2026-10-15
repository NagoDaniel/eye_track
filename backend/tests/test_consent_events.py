import gzip
import json
import zlib

import pytest

from app.consent import UnknownConsentVersion, consent_text, consent_versions
from app.events import (
    assemble_events,
    calibration_gate_failed,
    gunzip_limited,
    parse_events,
    parse_records,
)
from app.schemas import EventRecord, StimulusFrameRecord

LIMIT = 1_000_000


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
    jsonl = assemble_events([gz(a, b), gz(c)], max_inflated_bytes=LIMIT)
    assert [json.loads(x) for x in jsonl.splitlines()] == [a, b, c]
    assert [e.type for e in parse_events(jsonl)] == ["session_start", "video_play", "video_ended"]


def test_assemble_adds_missing_final_newline() -> None:
    chunk = gzip.compress(b'{"t": 1, "type": "session_start", "data": {}}')
    jsonl = assemble_events([chunk, chunk], max_inflated_bytes=LIMIT)
    assert len(jsonl.splitlines()) == 2


def test_assemble_names_the_bad_chunk() -> None:
    with pytest.raises(ValueError, match="events chunk 1 is not valid gzip"):
        assemble_events(
            [gz({"t": 1, "type": "session_start", "data": {}}), b"not gzip"],
            max_inflated_bytes=LIMIT,
        )


def test_parse_reports_the_bad_line() -> None:
    good = json.dumps({"t": 1, "type": "session_start", "data": {}}).encode()
    with pytest.raises(ValueError, match="events line 2"):
        parse_events(good + b"\n" + b'{"t": 2, "type": "mystery", "data": {}}\n')
    with pytest.raises(ValueError, match="events line 1"):
        parse_events(b"not json\n")


def test_corrupt_deflate_body_is_a_value_error_not_a_zlib_error() -> None:
    good = gzip.compress(b"x" * 1000)
    corrupt = good[:12] + bytes([good[12] ^ 0xFF]) + good[13:]
    with pytest.raises(zlib.error):  # what gzip.decompress would have raised
        gzip.decompress(corrupt)
    with pytest.raises(ValueError, match="events chunk 0 is not valid gzip"):
        assemble_events([corrupt], max_inflated_bytes=LIMIT)


def test_truncated_gzip_is_rejected() -> None:
    whole = gzip.compress(b'{"t": 1}\n' * 100)
    with pytest.raises(ValueError, match="truncated"):
        gunzip_limited(whole[:-10], LIMIT)


def test_trailing_bytes_after_the_gzip_stream_are_rejected() -> None:
    with pytest.raises(ValueError, match="unexpected bytes"):
        gunzip_limited(gzip.compress(b"abc") + b"junk", LIMIT)


def test_gzip_bomb_is_stopped_at_the_limit() -> None:
    bomb = gzip.compress(b"\0" * 20_000_000)
    assert len(bomb) < 50_000  # a tiny upload that would inflate to 20 MB
    with pytest.raises(ValueError, match="inflates to more than 1000000 bytes"):
        gunzip_limited(bomb, 1_000_000)
    with pytest.raises(ValueError, match="events chunk 0 .*inflates to more than"):
        assemble_events([bomb], max_inflated_bytes=1_000_000)


def test_exactly_the_limit_is_accepted() -> None:
    assert gunzip_limited(gzip.compress(b"a" * 1000), 1000) == b"a" * 1000
    with pytest.raises(ValueError, match="inflates to more than"):
        gunzip_limited(gzip.compress(b"a" * 1001), 1000)


def test_assemble_names_the_stream_in_its_error() -> None:
    with pytest.raises(ValueError, match="features chunk 1 is not valid gzip"):
        assemble_events([gz(), b"junk"], max_inflated_bytes=LIMIT, name="features")


def test_parse_records_validates_every_line_as_the_given_model() -> None:
    good = {"t": 1.0, "expected_display_time": 2.0, "media_time": 0.5, "presented_frames": 3}
    jsonl = (json.dumps(good) + "\n\n" + json.dumps(good) + "\n").encode()
    assert len(parse_records(jsonl, StimulusFrameRecord, "stimulus_frames")) == 2
    bad = jsonl + b'{"t": 1.0}\n'
    with pytest.raises(ValueError, match="stimulus_frames line 4 is invalid"):
        parse_records(bad, StimulusFrameRecord, "stimulus_frames")
    with pytest.raises(ValueError, match="line 1 is invalid"):
        parse_records(b"not json\n", StimulusFrameRecord, "stimulus_frames")


def gate_event(t: float, **data: object) -> EventRecord:
    base: dict[str, object] = {
        "block": "before_video",
        "attempt": 1,
        "passed": False,
        "skipped": False,
        "mean_error_norm": 0.2,
        "point_errors": [0.2, None],
        "valid_frames_per_dot": [10, 10],
        "reason": "high_error",
    }
    return EventRecord(t=t, type="calib_gate_result", data={**base, **data})


def test_calibration_gate_failed_uses_the_last_pre_video_result() -> None:
    assert calibration_gate_failed([]) is False
    assert calibration_gate_failed([gate_event(1)]) is True
    assert calibration_gate_failed([gate_event(1), gate_event(2, attempt=2, passed=True)]) is False
    assert calibration_gate_failed([gate_event(1, passed=True), gate_event(2, attempt=2)]) is True
    later = gate_event(3, block="after_last_video", passed=False)
    assert calibration_gate_failed([gate_event(1, passed=True), later]) is False
    skipped = gate_event(1, passed=None, skipped=True, reason="no_validation")
    assert calibration_gate_failed([skipped]) is False


def test_calibration_gate_failed_rejects_wrong_shapes() -> None:
    with pytest.raises(ValueError, match="calib_gate_result at t=1.0 is invalid"):
        calibration_gate_failed([gate_event(1.0, attempt=0)])
    with pytest.raises(ValueError, match="invalid"):
        calibration_gate_failed([gate_event(1.0, passed=None)])  # null verdict but not skipped
